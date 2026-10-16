| model                          |       size |     params | backend    | threads |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | ------: | --------------: | -------------------: |
| qwen3 8B Q4_K - Medium         |   4.68 GiB |     8.19 B | BLAS,MTL   |       8 |           pp512 |        368.68 ± 3.41 |
| qwen3 8B Q4_K - Medium         |   4.68 GiB |     8.19 B | BLAS,MTL   |       8 |           tg128 |         44.46 ± 0.42 |

build: 7fe450e19 (11146)
done
