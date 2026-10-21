package batchproto

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"reflect"
	"sort"
	"strings"
	"testing"

	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

// TestStateGoldenParity replays every scenario of testdata/state_golden.json (generated from the real
// Python BatchState by scratch/golden/gen_state_fixtures.py) against the Go port and compares, after
// EVERY op, the op's return value / raised error and a full snapshot of the state.

type stateGolden struct {
	Generator map[string]string `json:"generator"`
	Scenarios []struct {
		Name   string `json:"name"`
		Groups []struct {
			WarmPool string `json:"warmpool"`
			Size     int    `json:"size"`
			MinReady int    `json:"min_ready"`
		} `json:"groups"`
		NextOrdinal int `json:"next_ordinal"`
		Steps       []struct {
			Op     []json.RawMessage `json:"op"`
			Ret    json.RawMessage   `json:"ret"`
			Raises json.RawMessage   `json:"raises"`
			State  json.RawMessage   `json:"state"`
		} `json:"steps"`
	} `json:"scenarios"`
}

func errJSON(err error) any {
	if err == nil {
		return nil
	}
	var q *QuorumUnreachableError
	var tm *TerminalMemberError
	switch {
	case errors.As(err, &q):
		return map[string]any{"type": "QuorumUnreachable", "pool": q.WarmPool, "size": q.Size, "min_ready": q.MinReady,
			"failed": q.Failed, "lost": q.Lost, "released": q.Released}
	case errors.Is(err, ErrTimeout):
		return map[string]any{"type": "Timeout"}
	case errors.As(err, &tm):
		return map[string]any{"type": "TerminalMember", "claim": tm.Member.ClaimName, "state": string(tm.Member.State)}
	case errors.Is(err, ErrConsumerConflict), errors.Is(err, ErrMemberReleased):
		return map[string]any{"type": "BatchError"}
	case errors.Is(err, errNotMember):
		return map[string]any{"type": "ValueError"}
	}
	return map[string]any{"type": fmt.Sprintf("%T", err)}
}

func claimNames(ms []Member) []string {
	out := []string{}
	for _, m := range ms {
		out = append(out, m.ClaimName)
	}
	return out
}

func goSnapshot(s *batchState, pools []string) any {
	var quorum any
	if ms, err, ok := s.quorumResult(); ok {
		quorum = map[string]any{"members": claimNames(ms), "error": errJSON(err)}
	}
	gf := map[string]bool{}
	for _, p := range pools {
		gf[p] = s.groupFailed(p)
	}
	cnt := func(m map[string]int) map[string]int {
		out := map[string]int{}
		for _, p := range s.poolOrder {
			out[p] = m[p]
		}
		return out
	}
	return map[string]any{
		"members": s.membersSnapshot(""), "next_ordinal": s.nextOrdinal, "fill_settled": s.fillSettled(),
		"events_done": s.eventsDone(), "groups_done": s.groupsDone(), "quorum": quorum,
		"ready_count": s.readyCount, "unable": len(s.unable), "never_created": s.neverCreated,
		"failed": cnt(s.failed), "lost": cnt(s.lost), "released": cnt(s.releasedCount),
		"pending_deletes": sortedKeys(s.pendingDeletes), "group_failed": gf, "error": errJSON(s.err),
	}
}

func claimArg(t *testing.T, raw json.RawMessage) *extv1.SandboxClaim {
	var c extv1.SandboxClaim
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatal(err)
	}
	return &c
}

func claimsArg(t *testing.T, raw json.RawMessage) []*extv1.SandboxClaim {
	var rs []json.RawMessage
	if err := json.Unmarshal(raw, &rs); err != nil {
		t.Fatal(err)
	}
	var out []*extv1.SandboxClaim
	for _, r := range rs {
		out = append(out, claimArg(t, r))
	}
	return out
}

func str(t *testing.T, raw json.RawMessage) string {
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		t.Fatal(err)
	}
	return s
}

func applyOp(t *testing.T, s *batchState, op []json.RawMessage) (any, error) {
	switch k := str(t, op[0]); k {
	case "upsert":
		if m, ok := s.upsertClaim(claimArg(t, op[1])); ok {
			return string(m.State), nil
		}
		return nil, nil
	case "seed":
		s.seedFromClaims(claimsArg(t, op[1]))
		return nil, nil
	case "lost":
		if m, ok := s.markLost(str(t, op[1])); ok {
			return string(m.State), nil
		}
		return nil, nil
	case "resync":
		s.resyncFromList(claimsArg(t, op[1]))
		return nil, nil
	case "set_mode":
		mode := map[string]ConsumerMode{"stream": ModeStream, "group": ModeGroup, "quorum": ModeQuorum}[str(t, op[1])]
		return nil, s.setMode(mode)
	case "release_member":
		return s.releaseMember(str(t, op[1]))
	case "release_not_ready":
		return s.releaseNotReady(), nil
	case "release_group":
		n := s.releaseGroup(str(t, op[1]))
		if n == nil {
			n = []string{}
		}
		return n, nil
	case "start_acquire":
		return s.startAcquire(str(t, op[1])), nil
	case "acquired_member":
		m, err := s.acquiredMember(str(t, op[1]))
		if err != nil || m == nil {
			return nil, err
		}
		return m.ClaimName, nil
	case "drop_acquired":
		var created bool
		_ = json.Unmarshal(op[2], &created)
		return s.dropAcquired(str(t, op[1]), created), nil
	case "note_deleted":
		s.noteDeleted(str(t, op[1]))
		return nil, nil
	case "record_create_failure":
		s.recordCreateFailure(str(t, op[1]), str(t, op[2]), str(t, op[3]))
		return nil, nil
	case "record_skipped_create":
		s.recordSkippedCreate()
		return nil, nil
	case "expire_fill":
		return s.expireFill(), nil
	case "count_missing_fill":
		s.countMissingFillAsUnable()
		return nil, nil
	case "fail_quorum_timeout":
		ms, err := s.failQuorum(fmt.Errorf("x: %w", ErrTimeout))
		return map[string]any{"members": claimNames(ms), "error": errJSON(err)}, nil
	case "fill_creates_released":
		return s.fillCreatesReleased(str(t, op[1])), nil
	case "has_pool":
		return s.hasPool(str(t, op[1])), nil
	case "pop_events":
		out := []map[string]string{}
		for {
			ev, ok := s.popEvent()
			if !ok {
				break
			}
			out = append(out, map[string]string{"type": string(ev.Type), "claim": ev.Member.ClaimName, "state": string(ev.Member.State)})
		}
		return out, nil
	case "pop_groups":
		out := []map[string]any{}
		for {
			g, ok := s.popGroupOutcome()
			if !ok {
				break
			}
			out = append(out, map[string]any{"pool": g.WarmPool, "members": claimNames(g.Members), "error": errJSON(g.Err)})
		}
		return out, nil
	default:
		t.Fatalf("unknown op %q", k)
	}
	return nil, nil
}

// canon round-trips v through JSON so Go and Python values compare structurally.
func canon(t *testing.T, v any) any {
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func rawCanon(t *testing.T, raw json.RawMessage) any {
	if len(raw) == 0 {
		return nil
	}
	var out any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func sortEvents(v any) any {
	l, ok := v.([]any)
	if !ok {
		return v
	}
	sort.Slice(l, func(i, j int) bool { return fmt.Sprint(l[i]) < fmt.Sprint(l[j]) })
	return l
}

// knownStateDivergence lists scenarios where the Go port differs from Python's BatchState on purpose,
// pending a decision. Their mismatches are logged, not failed, and an entry that no longer diverges fails.
// Empty since decision B16 (the all-size-0 empty quorum on every handle, Python b3c49a08).
var knownStateDivergence = map[string]string{}

func TestStateGoldenParity(t *testing.T) {
	raw, err := os.ReadFile("testdata/state_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var g stateGolden
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	t.Logf("fixtures generated by %v", g.Generator)
	steps := 0
	for _, sc := range g.Scenarios {
		t.Run(sc.Name, func(t *testing.T) {
			var groups []BatchGroup
			var pools []string
			for _, gr := range sc.Groups {
				mr := gr.MinReady
				groups = append(groups, BatchGroup{WarmPool: gr.WarmPool, Size: gr.Size, MinReady: &mr})
				pools = append(pools, gr.WarmPool)
			}
			s := newBatchState("b", groups, sc.NextOrdinal)
			report := t.Errorf
			diverged := false
			if why, ok := knownStateDivergence[sc.Name]; ok {
				report = func(format string, args ...any) {
					diverged = true
					t.Logf("ALLOWED divergence (%s): %s", why, fmt.Sprintf(format, args...))
				}
				defer func() {
					if !diverged {
						t.Errorf("knownStateDivergence entry %q no longer diverges; remove it", sc.Name)
					}
				}()
			}
			unordered := strings.Contains(sc.Name, "order-insensitive")
			for i, st := range sc.Steps {
				steps++
				ret, err := applyOp(t, s, st.Op)
				gotRet, wantRet := canon(t, ret), rawCanon(t, st.Ret)
				if unordered {
					gotRet, wantRet = sortEvents(gotRet), sortEvents(wantRet)
				}
				if err != nil {
					gotRet = nil
				}
				id := fmt.Sprintf("step %d %s", i, st.Op[0])
				if !reflect.DeepEqual(gotRet, wantRet) {
					report("%s: ret\n  go:     %v\n  python: %v", id, gotRet, wantRet)
				}
				if got, want := canon(t, errJSON(err)), rawCanon(t, st.Raises); !reflect.DeepEqual(got, want) {
					report("%s: raises\n  go:     %v\n  python: %v", id, got, want)
				}
				if got, want := canon(t, goSnapshot(s, pools)), rawCanon(t, st.State); !reflect.DeepEqual(got, want) {
					gb, _ := json.Marshal(got)
					wb, _ := json.Marshal(want)
					report("%s: state\n  go:     %s\n  python: %s", id, gb, wb)
				}
			}
		})
	}
	t.Logf("replayed %d scenarios, %d steps", len(g.Scenarios), steps)
}
