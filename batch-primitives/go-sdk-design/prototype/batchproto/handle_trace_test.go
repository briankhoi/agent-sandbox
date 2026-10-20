package batchproto

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"reflect"
	"strings"
	"testing"
	"testing/synctest"
	"time"
)

// Handle-level golden traces (rev 3): the same timelines Python's real SandboxBatch consumers saw
// (golden_r3/gen_handle_traces.py -> testdata/handle_traces.json), replayed on miniHandle under synctest.
// Covers _next's wake-ups (broadcast, fill deadline, detach, err()), settle/end rules, call-again
// continuation, group and quorum outcomes. Times are compared in whole UNITs.
func TestHandleTraceParity(t *testing.T) {
	raw, err := os.ReadFile("testdata/handle_traces.json")
	if err != nil {
		t.Fatal(err)
	}
	var g struct {
		Generator struct {
			UnitSeconds float64 `json:"unit_seconds"`
		} `json:"generator"`
		Groups []struct {
			WarmPool string `json:"warmpool"`
			Size     int    `json:"size"`
			MinReady int    `json:"min_ready"`
		} `json:"groups"`
		Scenarios []struct {
			Name              string `json:"name"`
			Consumer          string `json:"consumer"`
			FillDeadlineUnits *int   `json:"fill_deadline_units"`
			Ops               []struct {
				Unit  int             `json:"unit"`
				Op    string          `json:"op"`
				Claim json.RawMessage `json:"claim"`
				Name  *string         `json:"name"`
			} `json:"ops"`
			Trace [][2]any `json:"trace"`
		} `json:"scenarios"`
	}
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	unit := time.Duration(g.Generator.UnitSeconds * float64(time.Second))
	var groups []BatchGroup
	for _, gr := range g.Groups {
		mr := gr.MinReady
		groups = append(groups, BatchGroup{gr.WarmPool, gr.Size, &mr})
	}
	errName := func(err error) string {
		var qu *QuorumUnreachableError
		switch {
		case errors.As(err, &qu):
			return "QuorumUnreachableError"
		case errors.Is(err, ErrTimeout):
			return "BatchTimeoutError"
		case errors.Is(err, ErrBatchClosed):
			return "BatchError"
		}
		return fmt.Sprintf("%T", err)
	}
	for _, sc := range g.Scenarios {
		t.Run(sc.Name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				h := newMiniHandle(newBatchState("hb", groups, 0))
				start := time.Now()
				if sc.FillDeadlineUnits != nil {
					h.fillDeadline = start.Add(time.Duration(*sc.FillDeadlineUnits) * unit)
				}
				var trace [][2]any
				at := func() float64 { return float64(time.Since(start) / unit) }
				rec := func(s string) { trace = append(trace, [2]any{at(), s}) }
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				done := make(chan struct{})
				go func() {
					defer close(done)
					switch sc.Consumer {
					case "events", "events-break-and-resume":
						prefix := ""
						if sc.Consumer != "events" {
							prefix = "iter1 "
							for ev, err := range h.Events(ctx) {
								if err != nil {
									rec("raise:" + errName(err))
									return
								}
								rec(fmt.Sprintf("%s%s:%s", prefix, ev.Type, ev.Member.ClaimName))
								break
							}
							prefix = "iter2 "
						}
						for ev, err := range h.Events(ctx) {
							if err != nil {
								if errors.Is(err, ErrBatchClosed) {
									rec("raise:BatchError")
								} else {
									rec("end+err") // Go yields the stopped error; Python's iterator ends with err() set
								}
								return
							}
							rec(fmt.Sprintf("%s%s:%s", prefix, ev.Type, ev.Member.ClaimName))
						}
						rec("end")
					case "groups":
						for o, err := range h.ReadyGroups(ctx) {
							if err != nil {
								rec("raise:" + errName(err))
								return
							}
							if o.Err != nil {
								rec(fmt.Sprintf("group:%s:ERR:%s", o.WarmPool, errName(o.Err)))
								continue
							}
							var names []string
							for _, m := range o.Members {
								names = append(names, m.ClaimName)
							}
							rec(fmt.Sprintf("group:%s:%s", o.WarmPool, strings.Join(names, ",")))
						}
						rec("end")
					case "quorum":
						ms, err := h.WaitForQuorum(ctx)
						if err != nil {
							rec("raise:" + errName(err))
							return
						}
						var names []string
						for _, m := range ms {
							names = append(names, m.ClaimName)
						}
						rec("quorum:" + strings.Join(names, ","))
					}
				}()
				for _, op := range sc.Ops {
					time.Sleep(time.Until(start.Add(time.Duration(op.Unit) * unit)))
					h.mu.Lock()
					switch op.Op {
					case "upsert":
						h.state.upsertClaim(claimArg(t, op.Claim))
					case "lost":
						h.state.markLost(*op.Name)
					case "error":
						h.state.noteError(errors.New("watch: forbidden"))
					case "detach":
						h.detached = true
					}
					h.broadcast()
					h.mu.Unlock()
				}
				select {
				case <-done:
				case <-time.After(20 * unit):
					rec("STILL BLOCKED")
					cancel()
					<-done
				}
				want := make([][2]any, len(sc.Trace))
				copy(want, sc.Trace)
				if !reflect.DeepEqual(trace, want) {
					t.Fatalf("trace mismatch\n  go:     %v\n  python: %v", trace, want)
				}
			})
		})
	}
	t.Logf("replayed %d handle scenarios", len(g.Scenarios))
}
