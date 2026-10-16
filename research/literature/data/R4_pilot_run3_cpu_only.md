| model                          |       size |     params | backend    | threads |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | ------: | --------------: | -------------------: |
| qwen3 8B Q4_K - Medium         |   4.68 GiB |     8.19 B | BLAS,MTL   |       8 |           pp128 |         37.86 ± 0.62 |
| qwen3 8B Q4_K - Medium         |   4.68 GiB |     8.19 B | BLAS,MTL   |       8 |            tg64 |         22.66 ± 0.73 |

build: 7fe450e19 (11146)
