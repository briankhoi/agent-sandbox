#!/bin/bash
set -euo pipefail
# Every model-checking run in results.md: NAME then mkcfg overrides.
cd "$(dirname "$0")"
rm -f ./*.cfg

F_CORE='Features={"crash","flaky","attach","detach"}'
F_REL='Features={"release","flaky","crash"}'
F_FENCE='Features={"release","detach","attach","flaky"}'
F_HAND='Features={"detach","attach","flaky"}'
F_SETUP='Features={"claimSetup","flaky","crash"}'
F_MEM='Features={"memberOps","flaky","crash"}'

# Positive suite (Bugs = {}, Fixes = {})
./mkcfg.sh main_core         "$F_CORE"  SkewMax=5 MaxTime=10
./mkcfg.sh main_release      "$F_REL"   SkewMax=5 MaxTime=10
./mkcfg.sh main_fence        "$F_FENCE" MaxTime=7
./mkcfg.sh main_claim_setup  "$F_SETUP" MaxTime=14
./mkcfg.sh main_member_ops   "$F_MEM"   MaxTime=14

# Clock-skew and tightness-boundary suite (Bugs = {}, Fixes = {})
./mkcfg.sh skew6_connected    'Features={}'                    SkewMax=6  MaxInc=1 MaxTime=14
./mkcfg.sh skew12_connected   'Features={}'                    SkewMax=12 MaxInc=1 MaxTime=14
./mkcfg.sh skew8_flaky        'Features={"flaky"}'             SkewMax=8  MaxInc=1 MaxTime=14
./mkcfg.sh skew9_flaky        'Features={"flaky"}'             SkewMax=9  MaxInc=1 MaxTime=14
./mkcfg.sh skew10_flaky       'Features={"flaky"}'             SkewMax=10 MaxInc=1 MaxTime=14
./mkcfg.sh skew8_member_flaky 'Features={"memberOps","flaky"}' SkewMax=8  MaxInc=2 MaxTime=10
./mkcfg.sh skew9_member_flaky 'Features={"memberOps","flaky"}' SkewMax=9  MaxInc=2 MaxTime=10

# Negative bug-injection suite (22/N regressions and protocol defenses)
./mkcfg.sh neg_unscopedReleaseDelete          'Features={"release"}'            'Bugs={"unscopedReleaseDelete"}' MaxTime=14
./mkcfg.sh neg_unscopedReleaseDelete_skipNone "$F_REL"                          'Bugs={"unscopedReleaseDelete"}' 'Fixes={"skipOnNone"}' SkewMax=5 MaxTime=10
./mkcfg.sh neg_reaperWaitInterval_skew6       'Features={}'                     SkewMax=6 'Bugs={"reaperWaitInterval"}' MaxInc=1 MaxTime=10
./mkcfg.sh neg_reaperWaitInterval_flaky_skew6 'Features={"flaky"}'              SkewMax=6 'Bugs={"reaperWaitInterval"}' MaxInc=1 MaxTime=10
./mkcfg.sh neg_unconditionalRollbackDelete    'Features={"claimSetup","flaky"}' 'Bugs={"unconditionalRollbackDelete"}' MaxTime=14
./mkcfg.sh neg_claimNoExpiryCheck             'Features={"claimSetup","flaky"}' 'Bugs={"claimNoExpiryCheck"}' MaxTime=14
./mkcfg.sh neg_retryPastExpiry                'Features={"memberOps","flaky"}'  'Bugs={"retryPastExpiry"}' MaxTime=14
./mkcfg.sh neg_reaperSubtracts                'Features={"flaky"}'              'Bugs={"reaperSubtracts"}' MaxInc=1 MaxTime=14
./mkcfg.sh neg_reaperSubtracts_interval       'Features={"flaky"}'              'Bugs={"reaperSubtracts","reaperWaitInterval"}' MaxInc=1 MaxTime=10
./mkcfg.sh neg_reaperNoPrecond                'Features={"release"}'            'Bugs={"reaperNoPrecondition"}' MaxTime=14
./mkcfg.sh neg_reaperNoPrecond_hand           "$F_HAND"                         'Bugs={"reaperNoPrecondition"}' MaxInc=1 MaxTime=14
./mkcfg.sh neg_reaperNoWait_skew              'Features={}'                     SkewMax=5 'Bugs={"reaperNoWait"}' MaxInc=1 MaxTime=10
./mkcfg.sh neg_reaperNoWait                   'Features={"flaky"}'              'Bugs={"reaperNoWait"}' MaxInc=1 MaxTime=10
./mkcfg.sh neg_expiryFromResp                 'Features={"flaky"}'              'Bugs={"expiryFromResponse"}' MaxInc=1 MaxTime=14
./mkcfg.sh neg_expiryFromResp_skew9           'Features={"flaky"}'              SkewMax=9 'Bugs={"expiryFromResponse"}' MaxInc=1 MaxTime=14
./mkcfg.sh neg_expiryFromResp_noWait          'Features={"flaky"}'              'Bugs={"expiryFromResponse","reaperNoWait"}' MaxInc=1 MaxTime=10
./mkcfg.sh neg_holderOnlyFence                "$F_FENCE"                        'Bugs={"holderOnlyFence"}' MaxTime=7
./mkcfg.sh neg_getBatchAdds                   "$F_HAND"                         'Bugs={"getBatchAdds"}' MaxInc=1 MaxTime=10
./mkcfg.sh neg_unconditionalRenew             "$F_HAND"                         'Bugs={"unconditionalRenew"}' MaxInc=1 MaxTime=10
