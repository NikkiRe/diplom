# T2: 5 прогонов на систему, системы в разные дни (ABAB) -> по сути непарные выборки.
import numpy as np
rng=np.random.default_rng(2)
def sim(n=5,s=0.3,R=3000,B=2000,true_ratio=1/3,paired=False):
    hit=0;w=[]
    for _ in range(R):
        a=rng.standard_normal(n); b=rng.standard_normal(n)
        A=np.exp(np.log(6)+s*a); Bv=np.exp(np.log(6*true_ratio)+s*b)
        ia=rng.integers(0,n,(B,n)); ib=ia if paired else rng.integers(0,n,(B,n))
        r=np.median(Bv[ib],axis=1)/np.median(A[ia],axis=1)
        lo,hi=np.percentile(r,[2.5,97.5]); hit+=(lo<=true_ratio<=hi); w.append(hi/lo)
    return hit/R,np.median(w)
for s in (0.2,0.3,0.5):
    c,w=sim(s=s); print(f"n=5 непарно, s={s}: покрытие={c:.3f}, ширина hi/lo={w:.2f} (×/÷{np.sqrt(w):.2f})")
