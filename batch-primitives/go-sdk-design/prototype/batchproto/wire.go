// Package batchproto is a throwaway prototype of the Go batch port (scratch only, not for merge).
// wire.go: the G1 "wire contract" functions, ported from Python TOP 8a72827f batch_utils.py/batch_state.py.
package batchproto

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"math/big"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

const (
	BatchIDLabel          = "agents.x-k8s.io/batch-id"
	BatchGroupLabel       = "agents.x-k8s.io/batch-group"
	BatchLeaseNamePrefix  = "batch-"
	createdByLabel        = "agents.x-k8s.io/created-by"
	groupSizeAnnotation   = "agents.x-k8s.io/batch-group-size"
	groupMinReadyAnnot    = "agents.x-k8s.io/batch-group-min-ready"
	leaseDurationAnnot    = "agents.x-k8s.io/batch-lease-duration"
	workBudgetAnnot       = "agents.x-k8s.io/batch-work-budget"
	quorumTimeoutAnnot    = "agents.x-k8s.io/batch-quorum-timeout"
	createRPSAnnot        = "agents.x-k8s.io/batch-create-rps"
	nextOrdinalAnnot      = "agents.x-k8s.io/batch-next-ordinal"
	clockSkewMargin       = 5
	defaultLeaseSeconds   = 60
	defaultQuorumSeconds  = 600
	defaultBudgetSeconds  = 3600
	defaultCreateRPS      = 50.0
	shutdownMarginSeconds = 600
	createAttempts        = 3
	backoffBase           = 0.5
	backoffCap            = 30.0
	batchIDMaxLen         = 52
	createFailedReason    = "CreateFailed"
)

var ErrInvalidBatchMetadata = errors.New("invalid batch metadata")

var batchIDRE = regexp.MustCompile(`^[a-z]([-a-z0-9]*[a-z0-9])?$`)

func validateBatchID(id string) error {
	// Go's regexp `$` is end-of-text (Python's re `$` also matches before a trailing "\n").
	if len(id) > batchIDMaxLen || !batchIDRE.MatchString(id) {
		return fmt.Errorf("batch id must be a DNS-1123 label starting with a letter, at most %d characters: %q", batchIDMaxLen, id)
	}
	return nil
}

func generateBatchID() string {
	const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
	b := []byte{'b'}
	for i := 0; i < 11; i++ {
		n, _ := rand.Int(rand.Reader, big.NewInt(int64(len(alphabet))))
		b = append(b, alphabet[n.Int64()])
	}
	return string(b)
}

func groupLabelValue(pool string) string {
	sum := sha256.Sum256([]byte(pool))
	return hex.EncodeToString(sum[:])[:16]
}

func groupLabelSelector(batchID, pool string) string {
	return BatchIDLabel + "=" + batchID + "," + BatchGroupLabel + "=" + groupLabelValue(pool)
}

type BatchGroup struct {
	WarmPool string
	Size     int
	MinReady *int
}

func (g BatchGroup) minReady() int {
	if g.MinReady == nil {
		return g.Size
	}
	return *g.MinReady
}

type planItem struct {
	ClaimName string
	Group     int // index into groups
}

// fillCreatePlan mirrors batch_utils.fill_create_plan: sort key (k >= minReady, Fraction(k,size), group index, k).
func fillCreatePlan(batchID string, groups []BatchGroup) []planItem {
	type slot struct{ late bool; k, size, gi int }
	first := make([]int, len(groups))
	var slots []slot
	acc := 0
	for i, g := range groups {
		first[i] = acc
		acc += g.Size
		for k := 0; k < g.Size; k++ {
			slots = append(slots, slot{k >= g.minReady(), k, g.Size, i})
		}
	}
	sort.Slice(slots, func(a, b int) bool {
		x, y := slots[a], slots[b]
		if x.late != y.late {
			return !x.late
		}
		// exact fraction compare k/size without floats
		if l, r := x.k*y.size, y.k*x.size; l != r {
			return l < r
		}
		if x.gi != y.gi {
			return x.gi < y.gi
		}
		return x.k < y.k
	})
	out := make([]planItem, len(slots))
	for i, s := range slots {
		out[i] = planItem{fmt.Sprintf("%s-%d", batchID, first[s.gi]+s.k), s.gi}
	}
	return out
}

// formatCreateRPS matches Python f"{x:g}" (6 significant digits).
func formatCreateRPS(v float64) string { return strconv.FormatFloat(v, 'g', 6, 64) }

type leaseSettings struct {
	LeaseDuration int               `json:"lease_duration"`
	QuorumTimeout int               `json:"quorum_timeout"`
	WorkBudget    int               `json:"work_budget"`
	NextOrdinal   int               `json:"next_ordinal"`
	CreateRPS     float64           `json:"create_rps"`
	Labels        map[string]string `json:"labels"`
}

func batchLeaseMetadata(labels map[string]string, batchID string, leaseDur, budget, quorum int, rps float64) (map[string]string, map[string]string) {
	l := map[string]string{}
	for k, v := range labels {
		l[k] = v
	}
	l[BatchIDLabel] = batchID
	l[createdByLabel] = "go-client"
	a := map[string]string{
		leaseDurationAnnot: strconv.Itoa(leaseDur),
		workBudgetAnnot:    strconv.Itoa(budget),
		quorumTimeoutAnnot: strconv.Itoa(quorum),
		createRPSAnnot:     formatCreateRPS(rps),
	}
	return l, a
}

func parseIntAnnotation(ann map[string]string, key string, def, min int) (int, error) {
	v, ok := ann[key]
	if !ok {
		return def, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < min {
		return 0, fmt.Errorf("%w: annotation %s value %q is not an integer of at least %d", ErrInvalidBatchMetadata, key, v, min)
	}
	return n, nil
}

// parseCreateRPS parses the batch-create-rps annotation. strconv.ParseFloat also accepts Go hex floats
// ("0x1p4" == 16, "0x1.8p1" == 3), which Python's float() rejects, so a hand-edited Lease Python refuses
// would be accepted by Go (F6). A hex float always has an x/X (its 0x prefix); no decimal or inf/nan
// spelling does, so rejecting those characters leaves Go never laxer than Python. Python stays laxer on
// surrounding whitespace and Unicode digits (allow-listed in the golden test); neither SDK writes those.
func parseCreateRPS(v string) (float64, bool) {
	if strings.ContainsAny(v, "xX") {
		return 0, false
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil || math.IsNaN(f) || math.IsInf(f, 0) || f <= 0 {
		return 0, false
	}
	return f, true
}

func parseLeaseMetadata(labels, ann map[string]string) (leaseSettings, error) {
	s := leaseSettings{CreateRPS: defaultCreateRPS, Labels: map[string]string{}}
	if v, ok := ann[createRPSAnnot]; ok {
		f, ok := parseCreateRPS(v)
		if !ok {
			return s, fmt.Errorf("%w: annotation %s value %q is not a positive number", ErrInvalidBatchMetadata, createRPSAnnot, v)
		}
		s.CreateRPS = f
	}
	var err error
	if s.LeaseDuration, err = parseIntAnnotation(ann, leaseDurationAnnot, defaultLeaseSeconds, clockSkewMargin+1); err != nil {
		return s, err
	}
	if s.QuorumTimeout, err = parseIntAnnotation(ann, quorumTimeoutAnnot, defaultQuorumSeconds, 1); err != nil {
		return s, err
	}
	if s.WorkBudget, err = parseIntAnnotation(ann, workBudgetAnnot, defaultBudgetSeconds, 1); err != nil {
		return s, err
	}
	if s.NextOrdinal, err = parseIntAnnotation(ann, nextOrdinalAnnot, 0, 0); err != nil {
		return s, err
	}
	for k, v := range labels {
		if k != BatchIDLabel && k != createdByLabel {
			s.Labels[k] = v
		}
	}
	return s, nil
}

type MemberState string

const (
	MemberPending MemberState = "pending"
	MemberReady   MemberState = "ready"
	MemberFailed  MemberState = "failed"
	MemberLost    MemberState = "lost"
)

type Member struct {
	ClaimName   string      `json:"claim_name"`
	SandboxName string      `json:"sandbox_name"`
	WarmPool    string      `json:"warmpool"`
	PodIPs      []string    `json:"pod_ips"`
	ServiceFQDN string      `json:"service_fqdn"`
	State       MemberState `json:"state"`
	Reason      string      `json:"reason"`
	Message     string      `json:"message"`
}

var terminalReasons = map[string]bool{
	"InvalidMetadata": true, "EnvVarsInjectionRejected": true, "VolumeClaimTemplatesError": true,
	"ClaimExpired": true, "SandboxExpired": true, "InvalidConfiguration": true, "PodFailed": true, "PodSucceeded": true,
}

// deriveMember mirrors batch_state.derive_member. It deliberately differs from the single-sandbox
// claimFailure(): no ObservedGeneration filter, and TemplateNotFound/WarmPoolNotFound are not terminal.
func deriveMember(c *extv1.SandboxClaim) Member {
	m := Member{ClaimName: c.Name, WarmPool: c.Spec.WarmPoolRef.Name, SandboxName: c.Status.SandboxStatus.Name, State: MemberPending, PodIPs: []string{}}
	for _, cond := range c.Status.Conditions {
		if cond.Type != "Ready" {
			continue
		}
		m.Reason, m.Message = cond.Reason, cond.Message
		if cond.Status == "True" {
			if m.SandboxName != "" {
				m.State = MemberReady
			}
		} else if cond.Status == "False" && terminalReasons[cond.Reason] {
			m.State = MemberFailed
		}
		break // first Ready condition only
	}
	terminating := c.DeletionTimestamp != nil
	if terminating && m.State != MemberFailed {
		m.State = MemberLost
	}
	if !terminating {
		m.PodIPs = append(m.PodIPs, c.Status.SandboxStatus.PodIPs...)
		m.ServiceFQDN = c.Status.SandboxStatus.ServiceFQDN
	}
	return m
}

// parseOrdinal mirrors batch_state.parse_ordinal (ASCII digits only; claim names are DNS labels).
func parseOrdinal(batchID, name string) (int, bool) {
	suffix, ok := strings.CutPrefix(name, batchID+"-")
	if !ok || suffix == "" {
		return 0, false
	}
	for _, r := range suffix {
		if r < '0' || r > '9' {
			return 0, false
		}
	}
	n, err := strconv.Atoi(suffix)
	return n, err == nil
}

type groupJSON struct {
	WarmPool string `json:"warmpool"`
	Size     int    `json:"size"`
	MinReady int    `json:"min_ready"`
}

func reconstructGroups(batchID string, claims []extv1.SandboxClaim) ([]groupJSON, error) {
	type key struct {
		none bool
		ord  int
	}
	less := func(a, b key) bool {
		if a.none != b.none {
			return !a.none
		}
		return a.ord < b.ord
	}
	byPool := map[string][2]string{}
	order := map[string]key{}
	for i := range claims {
		c := &claims[i]
		pool := c.Spec.WarmPoolRef.Name
		o, ok := parseOrdinal(batchID, c.Name)
		k := key{!ok, o}
		if cur, seen := order[pool]; !seen || less(k, cur) {
			order[pool] = k
		}
		size, hasSize := c.Annotations[groupSizeAnnotation]
		mr, hasMR := c.Annotations[groupMinReadyAnnot]
		if !hasSize && !hasMR {
			continue
		}
		if hasSize != hasMR {
			return nil, fmt.Errorf("%w: claim %q for pool %q carries only one of the batch group annotations", ErrInvalidBatchMetadata, c.Name, pool)
		}
		pair := [2]string{size, mr}
		if prev, seen := byPool[pool]; seen && prev != pair {
			return nil, fmt.Errorf("%w: pool %q has conflicting batch group annotations across its claims", ErrInvalidBatchMetadata, pool)
		}
		byPool[pool] = pair
	}
	pools := make([]string, 0, len(order))
	for p := range order {
		pools = append(pools, p)
	}
	sort.Slice(pools, func(i, j int) bool {
		a, b := order[pools[i]], order[pools[j]]
		if a != b {
			return less(a, b)
		}
		return pools[i] < pools[j]
	})
	out := []groupJSON{}
	for _, p := range pools {
		pair, ok := byPool[p]
		if !ok {
			out = append(out, groupJSON{p, 0, 0})
			continue
		}
		s, err1 := strconv.Atoi(pair[0])
		m, err2 := strconv.Atoi(pair[1])
		if err1 != nil || err2 != nil || s < 0 || m < 0 || m > s {
			return nil, fmt.Errorf("%w: pool %q has invalid batch group annotations (size=%q, min_ready=%q)", ErrInvalidBatchMetadata, p, pair[0], pair[1])
		}
		out = append(out, groupJSON{p, s, m})
	}
	return out, nil
}

// isLeaseStale: now > renew + (duration - skew).
func isLeaseStale(renew time.Time, durationSeconds int, now time.Time) bool {
	return now.After(renew.Add(time.Duration(durationSeconds-clockSkewMargin) * time.Second))
}

func isRetryableStatus(status int) bool { // status 0 == transport error
	return status == 0 || status == 429 || (status >= 500 && status < 600)
}

type createOutcome string

const (
	outcomeSuccess createOutcome = "success"
	outcomeRetry   createOutcome = "retry"
	outcomeFail    createOutcome = "fail"
)

// createErrorOutcome is Python's table. The Go port adds a GET-verify on 409 (see the design doc):
// a 409 on attempt 1 is only a collision if the existing claim isn't ours.
func createErrorOutcome(status, attempt int) createOutcome {
	if status == 409 {
		if attempt == 1 {
			return outcomeFail
		}
		return outcomeSuccess
	}
	if isRetryableStatus(status) && attempt < createAttempts {
		return outcomeRetry
	}
	return outcomeFail
}

func backoffDelay(attempt int, base, cap float64, r float64) float64 {
	d := math.Min(float64(attempt-1), math.Ceil(math.Log2(cap/base)))
	wait := math.Min(cap, base*math.Pow(2, d))
	return wait/2 + r*wait/2
}

// retryAfterSeconds returns the honoured Retry-After (ASCII digits only, capped at 30s).
func retryAfterSeconds(h string) (float64, bool) {
	h = strings.TrimSpace(h)
	if h == "" {
		return 0, false
	}
	for _, r := range h {
		if r < '0' || r > '9' {
			return 0, false
		}
	}
	n, err := strconv.ParseFloat(h, 64)
	if err != nil {
		return 0, false
	}
	return math.Min(n, backoffCap), true
}

// maxShutdownUnix is Python's datetime.max (9999-12-31T23:59:59.999999 UTC) in whole Unix seconds.
// construct_sandbox_claim_lifecycle_spec raises ValueError once now + seconds passes it (utils.py:91-96),
// and Go's metav1.Time can't marshal a year past 9999 either. Because the offset is whole seconds and
// datetime.now() has microseconds, Python's condition now+secs <= max is exactly
// secs <= maxShutdownUnix - now.Unix().
const maxShutdownUnix = 253402300799

// fillShutdownSeconds is claim_batch's shutdownTime offset, ceil(N/rps) + quorum + budget + 600
// (sandbox_batch.py:204-209). (rev 3, B4) It fails exactly where Python fails: N/rps is inf (math.ceil
// raises OverflowError; N > 0 with a tiny rps such as 5e-324), or now + offset passes 9999-12-31T23:59:59Z
// (ValueError). Revision 2 used a MaxInt32-second (~68 year) cap, which rejected rps 1e-10 and 4e-12
// that Python accepts. Python raises after its pool precheck GETs; Go returns the argument error first.
func fillShutdownSeconds(size int, rps float64, quorum, budget int, now time.Time) (int64, error) {
	limit := maxShutdownUnix - now.Unix()
	pace := math.Ceil(float64(size) / rps)
	if math.IsInf(pace, 0) || math.IsNaN(pace) || pace > float64(limit) {
		return 0, fmt.Errorf("create_rps %g is too small for %d claims: shutdownTime would be past year 9999", rps, size)
	}
	secs := int64(pace) + int64(quorum) + int64(budget) + shutdownMarginSeconds
	if secs < 0 || secs > limit { // < 0: a huge budget wrapped int64
		return 0, fmt.Errorf("shutdownTime %d seconds from now would be past year 9999", secs)
	}
	return secs, nil
}

// acquireShutdownSeconds is acquire's offset, quorum + budget + 600 (sandbox_batch.py:627-629), with the
// same bound. Only a hand-edited Lease (work_budget ~8e18 s fits Go's int64 parse) can reach it. Python
// raises at that point of acquire -- after taking an ordinal and a pacing slot -- and so does Go.
func acquireShutdownSeconds(quorum, budget int, now time.Time) (int64, error) {
	secs := int64(quorum) + int64(budget) + shutdownMarginSeconds
	if secs < 0 || secs > maxShutdownUnix-now.Unix() {
		return 0, fmt.Errorf("shutdownTime %d seconds from now would be past year 9999", secs)
	}
	return secs, nil
}

func formatShutdownTime(now time.Time, secs int64) string {
	return time.Unix(now.Unix()+secs, 0).UTC().Format("2006-01-02T15:04:05Z")
}
