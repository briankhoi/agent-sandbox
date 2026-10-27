package batchproto

// batch_state.go: a 1:1 port of Python TOP 8a72827f batch_state.BatchState (no I/O, no locking).
// Golden-tested against the Python class by state_golden_test.go. Python dicts are insertion-ordered,
// and several outputs depend on that order (quorum members, release_group's names, STREAM dispatch), so
// the port keeps insertion order explicitly (orderedKeys).

import (
	"errors"
	"fmt"
	"sort"

	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

type ConsumerMode int

const (
	modeNone ConsumerMode = iota
	ModeStream
	ModeGroup
	ModeQuorum
)

var consumerMethods = map[ConsumerMode]string{ModeStream: "events()", ModeGroup: "group_outcomes()", ModeQuorum: "wait_for_quorum()"}

type BatchEventType string

const (
	EventMemberReady  BatchEventType = "member_ready"
	EventMemberFailed BatchEventType = "member_failed"
	EventMemberLost   BatchEventType = "member_lost"
)

type BatchEvent struct {
	Type   BatchEventType
	Member Member
}

type GroupOutcome struct {
	WarmPool string
	Members  []Member
	Err      error
}

type QuorumUnreachableError struct {
	WarmPool                               string
	Size, MinReady, Failed, Lost, Released int
}

func (e *QuorumUnreachableError) Error() string {
	return fmt.Sprintf("group %q can't reach quorum: size=%d min_ready=%d failed=%d lost=%d released=%d",
		e.WarmPool, e.Size, e.MinReady, e.Failed, e.Lost, e.Released)
}

type TerminalMemberError struct{ Member Member }

func (e *TerminalMemberError) Error() string {
	return fmt.Sprintf("member %q is %s", e.Member.ClaimName, e.Member.State)
}

var (
	ErrConsumerConflict = errors.New("batch consumer mode conflict")
	ErrMemberReleased   = errors.New("batch member was released")
	errNotMember        = errors.New("not a member of the batch")
)

// orderedKeys is an insertion-ordered set of strings (Python dict/ordered-set semantics: re-adding an
// existing key keeps its position; deleting and re-adding moves it to the end).
type orderedKeys struct {
	keys []string
	has  map[string]bool
}

func newOrderedKeys() *orderedKeys { return &orderedKeys{has: map[string]bool{}} }
func (o *orderedKeys) add(k string) {
	if !o.has[k] {
		o.has[k] = true
		o.keys = append(o.keys, k)
	}
}
func (o *orderedKeys) remove(k string) {
	if !o.has[k] {
		return
	}
	delete(o.has, k)
	for i, x := range o.keys {
		if x == k {
			o.keys = append(o.keys[:i:i], o.keys[i+1:]...)
			return
		}
	}
}
func (o *orderedKeys) len() int               { return len(o.keys) }
func (o *orderedKeys) list() []string         { return append([]string(nil), o.keys...) }
func (o *orderedKeys) clear()                 { o.keys = nil; clear(o.has) }
func (o *orderedKeys) contains(k string) bool { return o.has[k] }

type batchState struct {
	batchID string
	groups  []BatchGroup
	size    int

	memberOrder *orderedKeys
	members     map[string]Member
	nextOrdinal int
	fillClaims  map[string]bool
	err         error

	poolOrder   []string // pools with size > 0, in group order (Python _group_by_pool iteration order)
	groupByPool map[string]BatchGroup
	minReady    map[string]int
	mode        ConsumerMode
	waiting     map[string]*orderedKeys
	dispatched  map[string]bool
	unable      map[string]bool
	failed      map[string]int
	lost        map[string]int

	neverCreated  int
	readyCount    int
	groupOutcomes map[string]GroupOutcome
	eventsQ       []BatchEvent
	outcomesQ     []GroupOutcome
	fillExpired   bool

	quorumDecided bool // Python: _quorum_members is not None
	quorumMembers []Member
	quorumErr     error
	groupsShort   map[string]bool

	released         map[string]bool
	releasedCount    map[string]int
	fillStoppedPools map[string]bool
	pendingDeletes   map[string]bool
	acquiringOrder   *orderedKeys
	acquiring        map[string]string
}

func newBatchState(batchID string, groups []BatchGroup, nextOrdinal int) *batchState {
	s := &batchState{
		batchID: batchID, groups: append([]BatchGroup(nil), groups...),
		memberOrder: newOrderedKeys(), members: map[string]Member{}, fillClaims: map[string]bool{},
		groupByPool: map[string]BatchGroup{}, minReady: map[string]int{}, waiting: map[string]*orderedKeys{},
		dispatched: map[string]bool{}, unable: map[string]bool{}, failed: map[string]int{}, lost: map[string]int{},
		groupOutcomes: map[string]GroupOutcome{}, groupsShort: map[string]bool{}, released: map[string]bool{},
		releasedCount: map[string]int{}, fillStoppedPools: map[string]bool{}, pendingDeletes: map[string]bool{},
		acquiringOrder: newOrderedKeys(), acquiring: map[string]string{},
	}
	for _, g := range groups {
		s.size += g.Size
		s.minReady[g.WarmPool] = g.minReady()
		if g.Size > 0 {
			if _, dup := s.groupByPool[g.WarmPool]; !dup {
				s.poolOrder = append(s.poolOrder, g.WarmPool)
			}
			s.groupByPool[g.WarmPool] = g
		}
	}
	s.nextOrdinal = max(s.size, nextOrdinal)
	for _, p := range s.poolOrder {
		s.waiting[p] = newOrderedKeys()
		s.failed[p], s.lost[p], s.releasedCount[p] = 0, 0, 0
		s.groupsShort[p] = true
	}
	return s
}

func hasFillAnnotations(c map[string]string) bool {
	_, a := c[groupSizeAnnotation]
	_, b := c[groupMinReadyAnnot]
	return a && b
}

func (s *batchState) seedFromClaims(claims []*extv1.SandboxClaim) {
	for i := range claims {
		s.upsertClaim(claims[i])
	}
}

func (s *batchState) putMember(name string, m Member) {
	s.members[name] = m
	s.memberOrder.add(name)
}

func (s *batchState) popMember(name string) (Member, bool) {
	m, ok := s.members[name]
	if ok {
		delete(s.members, name)
		s.memberOrder.remove(name)
	}
	return m, ok
}

func (s *batchState) upsertClaim(c *extv1.SandboxClaim) (Member, bool) {
	name := c.Name
	if s.released[name] {
		return Member{}, false
	}
	if o, ok := parseOrdinal(s.batchID, name); ok {
		s.nextOrdinal = max(s.nextOrdinal, o+1)
	}
	member := deriveMember(c)
	previous, hadPrev := s.members[name]
	if hasFillAnnotations(c.Annotations) {
		s.fillClaims[name] = true
		if !hadPrev && s.fillCreatesReleased(member.WarmPool) {
			s.releaseClaim(name, member.WarmPool)
			return Member{}, false
		}
	}
	s.putMember(name, member)
	var prev *Member
	if hadPrev {
		prev = &previous
	}
	s.onFillChange(name, member, prev)
	return member, true
}

func (s *batchState) markLost(name string) (Member, bool) {
	existing, ok := s.members[name]
	if !ok {
		return Member{}, false
	}
	state := MemberLost
	if existing.State == MemberFailed {
		state = MemberFailed
	}
	lostM := existing
	lostM.State, lostM.PodIPs, lostM.ServiceFQDN = state, []string{}, ""
	s.putMember(name, lostM)
	s.onFillChange(name, lostM, &existing)
	return lostM, true
}

// resyncFromList: Python iterates set(members) - seen, whose order is string-hash order (randomized per
// process). The Go port marks missing members lost in ordinal order, so its event order is deterministic.
func (s *batchState) resyncFromList(claims []*extv1.SandboxClaim) {
	seen := map[string]bool{}
	for _, c := range claims {
		seen[c.Name] = true
		s.upsertClaim(c)
	}
	var missing []string
	for _, n := range s.memberOrder.keys {
		if !seen[n] {
			missing = append(missing, n)
		}
	}
	s.sortByOrdinal(missing)
	for _, n := range missing {
		s.markLost(n)
	}
}

func (s *batchState) sortByOrdinal(names []string) {
	sort.SliceStable(names, func(i, j int) bool {
		a, aok := parseOrdinal(s.batchID, names[i])
		b, bok := parseOrdinal(s.batchID, names[j])
		if aok != bok {
			return aok
		}
		return a < b
	})
}

func (s *batchState) releaseClaim(name, pool string) {
	if s.released[name] {
		return
	}
	s.released[name] = true
	member, had := s.popMember(name)
	_, isAcq := s.acquiring[name]
	fill := !isAcq && (!had || s.isFill(name, member))
	delete(s.fillClaims, name)
	if !fill || s.unable[name] {
		return
	}
	s.unable[name] = true
	s.releasedCount[pool]++
	if had && member.State == MemberReady {
		s.readyCount--
	}
	if w := s.waiting[pool]; w != nil {
		w.remove(name)
	}
	switch s.mode {
	case ModeGroup:
		s.decideGroupOutcome(pool)
	case ModeQuorum:
		s.checkQuorum(pool)
	}
}

func (s *batchState) releaseMember(name string) (bool, error) {
	member, ok := s.members[name]
	if !ok {
		if s.released[name] {
			return s.pendingDeletes[name], nil
		}
		return false, fmt.Errorf("%q: %w %q", name, errNotMember, s.batchID)
	}
	s.releaseClaim(name, member.WarmPool)
	return len(s.takeDeletes([]Member{member})) > 0, nil
}

func (s *batchState) releaseNotReady() []string {
	for _, p := range s.poolOrder {
		s.fillStoppedPools[p] = true
	}
	var rel []Member
	for _, n := range s.memberOrder.keys {
		m := s.members[n]
		if s.isFill(n, m) && m.State != MemberReady && !s.dispatched[n] {
			rel = append(rel, m)
		}
	}
	for _, m := range rel {
		s.releaseClaim(m.ClaimName, m.WarmPool)
	}
	s.takeDeletes(rel)
	return sortedKeys(s.pendingDeletes)
}

func (s *batchState) releaseGroup(pool string) []string {
	s.fillStoppedPools[pool] = true
	var names []string
	for _, n := range s.memberOrder.keys {
		if s.members[n].WarmPool == pool {
			names = append(names, n)
		}
	}
	var acq []string
	for _, n := range s.acquiringOrder.keys {
		if s.acquiring[n] == pool {
			acq = append(acq, n)
		}
	}
	for _, n := range append(append([]string(nil), names...), acq...) {
		s.releaseClaim(n, pool)
	}
	for _, n := range acq {
		s.pendingDeletes[n] = true
	}
	return names
}

func (s *batchState) hasPool(pool string) bool {
	for _, g := range s.groups {
		if g.WarmPool == pool {
			return true
		}
	}
	for _, m := range s.members {
		if m.WarmPool == pool {
			return true
		}
	}
	for _, p := range s.acquiring {
		if p == pool {
			return true
		}
	}
	return false
}

func (s *batchState) takeDeletes(released []Member) []string {
	var names []string
	for _, m := range released {
		if m.State != MemberLost && m.Reason != createFailedReason {
			names = append(names, m.ClaimName)
			s.pendingDeletes[m.ClaimName] = true
		}
	}
	return names
}

func (s *batchState) startAcquire(pool string) string {
	name := fmt.Sprintf("%s-%d", s.batchID, s.nextOrdinal)
	s.nextOrdinal++
	s.acquiring[name] = pool
	s.acquiringOrder.add(name)
	return name
}

func (s *batchState) acquiredMember(name string) (*Member, error) {
	if s.released[name] {
		return nil, fmt.Errorf("acquired member %q was released before it was Ready: %w", name, ErrMemberReleased)
	}
	m, ok := s.members[name]
	if !ok || m.State == MemberPending {
		return nil, nil
	}
	if m.State == MemberReady {
		delete(s.acquiring, name)
		s.acquiringOrder.remove(name)
		return &m, nil
	}
	return nil, &TerminalMemberError{Member: m}
}

func (s *batchState) dropAcquired(name string, created bool) bool {
	delete(s.acquiring, name)
	s.acquiringOrder.remove(name)
	if s.released[name] {
		if !created {
			delete(s.pendingDeletes, name)
		}
		return created && s.pendingDeletes[name]
	}
	s.released[name] = true
	member, had := s.popMember(name)
	if !created {
		return false
	}
	if !had {
		s.pendingDeletes[name] = true
		return true
	}
	return len(s.takeDeletes([]Member{member})) > 0
}

func (s *batchState) noteDeleted(name string)              { delete(s.pendingDeletes, name) }
func (s *batchState) fillCreatesReleased(pool string) bool { return s.fillStoppedPools[pool] }

func (s *batchState) membersSnapshot(pool string) []Member {
	var names []string
	for _, n := range s.memberOrder.keys {
		if pool == "" || s.members[n].WarmPool == pool {
			names = append(names, n)
		}
	}
	s.sortByOrdinal(names)
	out := make([]Member, 0, len(names))
	for _, n := range names {
		out = append(out, s.members[n])
	}
	return out
}

func (s *batchState) noteError(err error) {
	if s.err == nil {
		s.err = err
	}
}

func (s *batchState) isFill(name string, m Member) bool {
	_, known := s.groupByPool[m.WarmPool]
	return s.fillClaims[name] && known
}

func (s *batchState) onFillChange(name string, member Member, previous *Member) {
	if !s.isFill(name, member) {
		return
	}
	pool := member.WarmPool
	wasReady := previous != nil && previous.State == MemberReady && !s.unable[name]
	switch {
	case !s.unable[name] && (member.State == MemberFailed || member.State == MemberLost):
		s.unable[name] = true
		if wasReady {
			s.readyCount--
		}
		ev := EventMemberFailed
		if member.State == MemberLost {
			s.lost[pool]++
			ev = EventMemberLost
		} else {
			s.failed[pool]++
		}
		s.waiting[pool].remove(name)
		s.eventsQ = append(s.eventsQ, BatchEvent{Type: ev, Member: member})
	case !s.unable[name] && member.State == MemberReady:
		if !wasReady {
			s.readyCount++
		}
		if !s.dispatched[name] {
			s.routeReadyMember(name, pool)
		}
	case member.State != MemberReady:
		if wasReady {
			s.readyCount--
		}
		s.waiting[pool].remove(name)
	}
	switch s.mode {
	case ModeGroup:
		s.decideGroupOutcome(pool)
	case ModeQuorum:
		s.checkQuorum(pool)
	}
}

func (s *batchState) routeReadyMember(name, pool string) {
	switch s.mode {
	case ModeStream:
		s.dispatch(name)
	case ModeQuorum:
		if s.quorumDecided {
			s.dispatch(name)
		} else if s.quorumErr == nil {
			s.waiting[pool].add(name)
		}
	default:
		outcome, ok := s.groupOutcomes[pool]
		if !ok {
			s.waiting[pool].add(name)
		} else if outcome.Err == nil {
			s.dispatch(name)
		}
	}
}

func (s *batchState) dispatch(name string) {
	s.dispatched[name] = true
	s.eventsQ = append(s.eventsQ, BatchEvent{Type: EventMemberReady, Member: s.members[name]})
}

func (s *batchState) decideGroupOutcome(pool string) {
	if _, done := s.groupOutcomes[pool]; done {
		return
	}
	mr := s.minReady[pool]
	w := s.waiting[pool]
	if w.len() >= mr {
		names := w.list()
		w.clear()
		cohort := names[:mr]
		ms := make([]Member, 0, mr)
		for _, n := range cohort {
			s.dispatched[n] = true
			ms = append(ms, s.members[n])
		}
		s.setGroupOutcome(GroupOutcome{WarmPool: pool, Members: ms})
		for _, n := range names[mr:] {
			s.dispatch(n)
		}
	} else if err := s.unreachableError(pool); err != nil {
		w.clear()
		s.setGroupOutcome(GroupOutcome{WarmPool: pool, Err: err})
	}
}

func (s *batchState) unreachableError(pool string) *QuorumUnreachableError {
	g := s.groupByPool[pool]
	mr := s.minReady[pool]
	unable := s.failed[pool] + s.lost[pool] + s.releasedCount[pool]
	if g.Size-unable >= mr {
		return nil
	}
	return &QuorumUnreachableError{WarmPool: pool, Size: g.Size, MinReady: mr, Failed: s.failed[pool], Lost: s.lost[pool], Released: s.releasedCount[pool]}
}

func (s *batchState) setGroupOutcome(o GroupOutcome) {
	s.groupOutcomes[o.WarmPool] = o
	s.outcomesQ = append(s.outcomesQ, o)
}

func (s *batchState) timeOutUndecidedGroups() {
	for _, p := range s.poolOrder {
		if _, ok := s.groupOutcomes[p]; !ok {
			s.waiting[p].clear()
			s.setGroupOutcome(GroupOutcome{WarmPool: p, Err: fmt.Errorf("group quorum timed out: %w", ErrTimeout)})
		}
	}
}

func (s *batchState) checkQuorum(pool string) {
	if s.quorumDecided || s.quorumErr != nil {
		return
	}
	if s.waiting[pool].len() < s.minReady[pool] {
		s.groupsShort[pool] = true
		if err := s.unreachableError(pool); err != nil {
			s.failQuorum(err)
		}
		return
	}
	delete(s.groupsShort, pool)
	if len(s.groupsShort) > 0 {
		return
	}
	var quorum, extras []string
	for _, p := range s.poolOrder {
		names := s.waiting[p].list()
		s.waiting[p].clear()
		mr := s.minReady[p]
		quorum = append(quorum, names[:mr]...)
		extras = append(extras, names[mr:]...)
	}
	s.quorumDecided = true
	s.quorumMembers = []Member{}
	for _, n := range quorum {
		s.dispatched[n] = true
		s.quorumMembers = append(s.quorumMembers, s.members[n])
	}
	for _, n := range extras {
		s.dispatch(n)
	}
}

func (s *batchState) failQuorum(err error) ([]Member, error) {
	if !s.quorumDecided && s.quorumErr == nil {
		s.quorumErr = err
		for _, p := range s.poolOrder {
			s.waiting[p].clear()
		}
	}
	return s.quorumResultMembers(), s.quorumErr
}

func (s *batchState) quorumResultMembers() []Member {
	if s.quorumMembers == nil {
		return []Member{}
	}
	return s.quorumMembers
}

// quorumResult returns (members, err, decided).
func (s *batchState) quorumResult() ([]Member, error, bool) {
	if !s.quorumDecided && s.quorumErr == nil {
		return nil, nil, false
	}
	return s.quorumResultMembers(), s.quorumErr, true
}

func (s *batchState) setMode(mode ConsumerMode) error {
	if s.mode != modeNone {
		if mode == ModeStream || (mode == s.mode && mode == ModeGroup) {
			return nil
		}
		if mode == s.mode && mode == ModeQuorum {
			return fmt.Errorf("wait_for_quorum() can only be called once: %w", ErrConsumerConflict)
		}
		return fmt.Errorf("%s can't be used after %s: %w", consumerMethods[mode], consumerMethods[s.mode], ErrConsumerConflict)
	}
	s.mode = mode
	switch mode {
	case ModeStream:
		for _, p := range s.poolOrder {
			for _, n := range s.waiting[p].list() {
				s.dispatch(n)
			}
			s.waiting[p].clear()
		}
	case ModeGroup:
		for _, p := range s.poolOrder {
			s.decideGroupOutcome(p)
		}
		if s.fillExpired {
			s.timeOutUndecidedGroups()
		}
	case ModeQuorum:
		for _, p := range s.poolOrder {
			s.checkQuorum(p)
		}
		// Decision B16 (Python b3c49a08, batch_state.py:680-683): with no group of size > 0 there is no
		// fill to wait for and no watch event would ever decide the quorum, so it is reached with no
		// members, on every handle (ClaimBatch or GetBatch). Set unconditionally, as Python does.
		if len(s.groupByPool) == 0 {
			s.quorumDecided = true
			s.quorumMembers = []Member{}
		}
		if s.fillExpired {
			s.failQuorum(fmt.Errorf("batch quorum timed out: %w", ErrTimeout))
		}
	}
	return nil
}

func (s *batchState) recordCreateFailure(name, pool, message string) {
	if _, ok := s.members[name]; ok || s.released[name] {
		return
	}
	if s.fillCreatesReleased(pool) {
		s.releaseClaim(name, pool)
		return
	}
	s.fillClaims[name] = true
	m := Member{ClaimName: name, WarmPool: pool, State: MemberFailed, Reason: createFailedReason, Message: message, PodIPs: []string{}}
	s.putMember(name, m)
	s.onFillChange(name, m, nil)
}

func (s *batchState) recordSkippedCreate() { s.neverCreated++ }

func (s *batchState) groupFailed(pool string) bool {
	if s.mode == ModeQuorum {
		return s.quorumErr != nil
	}
	o, ok := s.groupOutcomes[pool]
	return s.mode == ModeGroup && ok && o.Err != nil
}

func (s *batchState) expireFill() bool {
	if s.fillExpired {
		return false
	}
	s.fillExpired = true
	switch s.mode {
	case ModeGroup:
		s.timeOutUndecidedGroups()
	case ModeQuorum:
		s.failQuorum(fmt.Errorf("batch quorum timed out: %w", ErrTimeout))
	}
	return true
}

func (s *batchState) countMissingFillAsUnable() {
	seen := map[string]int{}
	for _, n := range s.memberOrder.keys {
		m := s.members[n]
		if s.isFill(n, m) {
			seen[m.WarmPool]++
		}
	}
	for _, p := range s.poolOrder {
		missing := max(0, s.groupByPool[p].Size-seen[p])
		s.lost[p] += missing
		s.neverCreated += missing
	}
}

func (s *batchState) popEvent() (BatchEvent, bool) {
	for len(s.eventsQ) > 0 {
		ev := s.eventsQ[0]
		s.eventsQ = s.eventsQ[1:]
		if ev.Type == EventMemberReady && s.released[ev.Member.ClaimName] {
			continue
		}
		return ev, true
	}
	return BatchEvent{}, false
}

func (s *batchState) popGroupOutcome() (GroupOutcome, bool) {
	if len(s.outcomesQ) == 0 {
		return GroupOutcome{}, false
	}
	o := s.outcomesQ[0]
	s.outcomesQ = s.outcomesQ[1:]
	return o, true
}

func (s *batchState) fillSettled() bool {
	return s.fillExpired || s.readyCount+len(s.unable)+s.neverCreated >= s.size
}
func (s *batchState) eventsDone() bool { return s.fillSettled() && len(s.eventsQ) == 0 }
func (s *batchState) groupsDone() bool {
	return len(s.groupOutcomes) == len(s.groupByPool) && len(s.outcomesQ) == 0
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k, v := range m {
		if v {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}
