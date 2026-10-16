| model                          |       size |     params | backend    | threads |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | ------: | --------------: | -------------------: |
| qwen3 8B Q4_K - Medium         |   4.68 GiB |     8.19 B | BLAS,MTL   |       8 |           tg128 |         44.50 ± 0.35 |
| qwen3 8B Q4_K - Medium         |   4.68 GiB |     8.19 B | BLAS,MTL   |       8 |   tg128 @ d4096 |         39.56 ± 0.32 |

build: 7fe450e19 (11146)
