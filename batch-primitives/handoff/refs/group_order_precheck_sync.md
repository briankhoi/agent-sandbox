# Group order into PR 2 + acquire precheck single-flight in PR 6: sync review copies to real branches

Run only after Brian approves. One atomic push; each lease pins the real branch head this restack was built on (after the 4/N merge restack onto upstream 4bb0db30, 2026-10-21). fu3..fu9 are unchanged. Review heads: round 3 (R1 owner TimeoutError, R2/R3 tests).

```sh
git fetch origin
git push --atomic origin \
  --force-with-lease=feat/batch-2-claim:5693448af9d583e68e9c571ffbe113ee490d3bdf \
  3609a73f4244c904785dd2d8dc06e024ca55ddcf:refs/heads/feat/batch-2-claim \
  --force-with-lease=feat/batch-3-group-consumers:4e036c6f2d7d74a0694abdb356879f6630e69956 \
  aed55b4224e0dd89a76d77e50d86e721bb2ffe16:refs/heads/feat/batch-3-group-consumers \
  --force-with-lease=feat/batch-4-group-quorum:3d9b3651fbfd8a3509fb3d79c4ba817601a24082 \
  a91dd880a9a5214c1048391d5a2b6848c58058c7:refs/heads/feat/batch-4-group-quorum \
  --force-with-lease=feat/batch-5-release-group-76a8m3:7ba009bb4a8790d9b6a71dfa5fd9ec5a0d3a5d90 \
  c02229bc69ffc9a914e1e25a2e29d967ec1f900a:refs/heads/feat/batch-5-release-group-76a8m3 \
  --force-with-lease=feat/batch-6-acquire-nbbp5j:446b26cf018a5a0c434ef67ec00baa9052944217 \
  c227f2f44f704852ef55eb4eac84a2ac1e370b45:refs/heads/feat/batch-6-acquire-nbbp5j \
  --force-with-lease=feat/batch-transport-ltlfdm:06728f6d4f3ebe26c35d42f235732afd2517b106 \
  cfc74d0c16c3c4989da44e4a7b6965007a926d17:refs/heads/feat/batch-transport-ltlfdm \
  --force-with-lease=feat/batch-reaper-ymtd6t:a4bb53747f30772152d7c126e6766504c74c33fa \
  268c988a8945a20221d75d7b0f43f2da9d7e80be:refs/heads/feat/batch-reaper-ymtd6t \
  --force-with-lease=feat/batch-7a-create-path:0a2cd6d52e831d64f90acfc451482513dfa15c61 \
  769b7f2e6ecac8ae1b2351e11b70c78838845b80:refs/heads/feat/batch-7a-create-path
```
