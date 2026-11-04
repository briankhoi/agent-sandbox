#!/bin/bash
# Every model-checking run in results.md: NAME then mkcfg overrides.
cd "$(dirname "$0")"
F_CORE='Features={"crash","flaky","attach","detach"}'
F_REL='Features={"release","flaky","crash"}'
F_FENCE='Features={"release","detach","attach","flaky"}'
F_HAND='Features={"detach","attach","flaky"}'
./mkcfg.sh main_core      "$F_CORE" 'Fixes={}'
./mkcfg.sh main_release   "$F_REL"  'Fixes={"uidScoped"}'
./mkcfg.sh main_fence     "$F_FENCE" 'Fixes={"uidScoped"}' MaxTime=7
./mkcfg.sh finding_23n    'Features={"release"}' 'Fixes={}'
./mkcfg.sh fix_skipOnNone "$F_REL"  'Fixes={"skipOnNone"}'
./mkcfg.sh skew5_connected 'Features={}' SkewMax=5 'Fixes={}' MaxInc=1
./mkcfg.sh skew6_connected 'Features={}' SkewMax=6 'Fixes={}' MaxInc=1
./mkcfg.sh skew6_connected_waitLonger  'Features={}' SkewMax=6 'Fixes={"reaperWaitLonger"}' MaxInc=1
./mkcfg.sh skew12_connected_waitLonger 'Features={}' SkewMax=12 'Fixes={"reaperWaitLonger"}' MaxInc=1
./mkcfg.sh skew5_flaky     'Features={"flaky"}' SkewMax=5 'Fixes={}' MaxInc=1
./mkcfg.sh skew6_flaky     'Features={"flaky"}' SkewMax=6 'Fixes={}' MaxInc=1
./mkcfg.sh neg_reaperSubtracts   'Features={"flaky"}' 'Bugs={"reaperSubtracts"}' 'Fixes={}' MaxInc=1
./mkcfg.sh neg_reaperNoPrecond   'Features={"release"}' 'Bugs={"reaperNoPrecondition"}' 'Fixes={"uidScoped"}'
./mkcfg.sh neg_reaperNoWait_skew 'Features={}' SkewMax=5 'Bugs={"reaperNoWait"}' 'Fixes={}' MaxInc=1
./mkcfg.sh neg_reaperNoWait      'Features={"flaky"}' 'Bugs={"reaperNoWait"}' 'Fixes={}' MaxInc=1
./mkcfg.sh neg_expiryFromResp    'Features={"flaky"}' 'Bugs={"expiryFromResponse"}' 'Fixes={}' MaxInc=1
./mkcfg.sh neg_expiryFromResp_noWait 'Features={"flaky"}' 'Bugs={"expiryFromResponse","reaperNoWait"}' 'Fixes={}' MaxInc=1
./mkcfg.sh neg_holderOnlyFence   "$F_FENCE" 'Bugs={"holderOnlyFence"}' 'Fixes={"uidScoped"}' MaxTime=7
./mkcfg.sh neg_getBatchAdds      "$F_HAND" 'Bugs={"getBatchAdds"}' 'Fixes={}' MaxInc=1
./mkcfg.sh neg_reaperNoPrecond_hand "$F_HAND" 'Bugs={"reaperNoPrecondition"}' 'Fixes={}' MaxInc=1
./mkcfg.sh neg_unconditionalRenew "$F_HAND" 'Bugs={"unconditionalRenew"}' 'Fixes={}' MaxInc=1
