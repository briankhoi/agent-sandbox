package batchproto

import (
	"encoding/json"
	"os"
	"testing"
	"time"
)

// B4 (rev 3): Go rejects a shutdownTime exactly where Python TOP does. Fixtures from
// golden_r3/gen_shutdown_bound.py, which calls the real construct_sandbox_claim_lifecycle_spec with
// claim_batch's and acquire's exact expressions and a frozen `now`; the same instant is passed here.
func TestShutdownBoundGolden(t *testing.T) {
	raw, err := os.ReadFile("testdata/shutdown_bound.json")
	if err != nil {
		t.Fatal(err)
	}
	type result struct {
		OK           bool   `json:"ok"`
		ShutdownTime string `json:"shutdown_time"`
		ErrorType    string `json:"error_type"`
	}
	var g struct {
		Now  string `json:"now"`
		Fill []struct {
			Size          int     `json:"size"`
			CreateRPS     float64 `json:"create_rps"`
			QuorumTimeout int     `json:"quorum_timeout"`
			WorkBudget    int     `json:"work_budget"`
			Result        result  `json:"result"`
		} `json:"fill"`
		Acquire []struct {
			QuorumTimeout int    `json:"quorum_timeout"`
			WorkBudget    int    `json:"work_budget"`
			Result        result `json:"result"`
		} `json:"acquire"`
	}
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	now, err := time.Parse(time.RFC3339Nano, g.Now)
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, c := range g.Fill {
		n++
		secs, err := fillShutdownSeconds(c.Size, c.CreateRPS, c.QuorumTimeout, c.WorkBudget, now)
		got := ""
		if err == nil {
			got = formatShutdownTime(now, secs)
		}
		if c.Result.OK != (err == nil) || (err == nil && got != c.Result.ShutdownTime) {
			t.Errorf("fill N=%d rps=%g q=%d b=%d: go=(%q, %v) python=%+v", c.Size, c.CreateRPS, c.QuorumTimeout, c.WorkBudget, got, err, c.Result)
		}
	}
	for _, c := range g.Acquire {
		n++
		secs, err := acquireShutdownSeconds(c.QuorumTimeout, c.WorkBudget, now)
		got := ""
		if err == nil {
			got = formatShutdownTime(now, secs)
		}
		if c.Result.OK != (err == nil) || (err == nil && got != c.Result.ShutdownTime) {
			t.Errorf("acquire q=%d b=%d: go=(%q, %v) python=%+v", c.QuorumTimeout, c.WorkBudget, got, err, c.Result)
		}
	}
	t.Logf("checked %d shutdown-bound cases against Python (now=%s)", n, g.Now)
}
