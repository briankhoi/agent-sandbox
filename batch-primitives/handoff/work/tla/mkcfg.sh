#!/bin/bash
# usage: mkcfg.sh NAME key=value ... ; writes NAME.cfg from defaults plus overrides
name=$1; shift
declare -A c=([D]=6 [Grace]=6 [G]=1 [M]=2 [SkewMax]=2 [Lat]=1 [MaxTime]=10 [MaxInc]=2 [MaxRounds]=2 [Features]="{\"attach\",\"detach\",\"release\",\"crash\",\"flaky\"}" [Bugs]='{}' [Fixes]='{"uidScoped"}')
inv="ReaperSafety ReaperSafetyConnected AtMostOneBeliever BelieverIsHolder BelieverKeepsClaims ReleaseFencing"
for kv in "$@"; do k=${kv%%=*}; v=${kv#*=}; if [ "$k" = INV ]; then inv=$v; else c[$k]=$v; fi; done
{
echo "SPECIFICATION Spec"
echo "CONSTANTS"
for k in D Grace G M SkewMax Lat MaxTime MaxInc MaxRounds Features Bugs Fixes; do echo "  $k = ${c[$k]}"; done
echo "  Procs = {h1, h2}"
echo "  NoOne = none"
echo "SYMMETRY Symm"
echo "INVARIANTS"
for i in $inv; do echo "  $i"; done
} > $name.cfg
