# Local AI validation - 2026-10-11

## Completed locally

- Backend suite: 136 tests passed with local AI disabled; 138 with it enabled.
- JSX compilation passed. Desktop/mobile browser checks passed with external
  requests blocked, including login, local model selection and a real answer.
- Real API checks passed for authentication, concurrent-request rejection (429)
  and oversized-context rejection (400).
- Model download SHA-256 verified. Inference image built and started on the ARM Mac.
- Container settings verified: one CPU-equivalent quota, 3 GiB memory, equal
  memory/swap ceiling, read-only filesystem, no published inference port.
- Outbound TCP connection from the inference container was blocked.

## Limitations found

- The initial two-file Python benchmark took 18.9 seconds on the Mac, but the model
  returned unchanged buggy code while claiming it was fixed. A simpler square
  function correction passed. Model quality has not met the acceptance criteria.
- A forced one-second request deadline returned 504 in approximately 1.01 seconds.
  Output-generation cleanup released the slot after about three seconds. During
  long prompt processing, logs showed cancellation/release delays around 12-28
  seconds; short observation checks failed. Cleanup is not an instantaneous CPU
  stop. The admission guard rejects requests while inference remains occupied.
- Lenovo speed, memory consumption and effect on other services are untested.

## Next step

Run one isolated benchmark on the Lenovo, retaining the existing resource limits.
First verify its Docker Compose version and disk space. Use a separate project,
database and loopback port; do not start the base Compose file on its production
port for this test. Review actual generated fixes as well as response times.

No commit, push, PR, merge or production deployment has been performed.
