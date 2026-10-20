package batchproto

import (
	"k8s.io/client-go/rest"
)

// batchRESTConfigs derives the configs a batch handle builds its clients from (design §3.5), all used
// with NewForConfigAndClient on the K8sHelper's one shared *http.Client.
//
//   - ext (claims): if the caller left both QPS and RateLimiter unset, QPS=-1 (no client-side token
//     bucket; the SDK paces creates with CreateRPS). Otherwise the caller's limiter policy is kept.
//   - lease: never rate limited, and never shares the caller's RateLimiter. RESTClientForConfigAndClient
//     uses config.RateLimiter as-is whenever it is non-nil (client-go rest/config.go:373-386), so a plain
//     copy would queue renewals behind creates in the caller's bucket (F7). Renewals are at most one per
//     D/3 seconds, plus detach.
func batchRESTConfigs(user *rest.Config) (ext, lease *rest.Config) {
	ext = rest.CopyConfig(user)
	if ext.QPS == 0 && ext.RateLimiter == nil {
		ext.QPS = -1
	}
	lease = rest.CopyConfig(user)
	lease.RateLimiter = nil
	lease.QPS = -1
	return ext, lease
}
