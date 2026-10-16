# Покрытие 95% перцентильного парного бутстрепа для отношения медиан при n=10 суток
import numpy as np
rng=np.random.default_rng(1)
def sim(n=10,s=0.3,rho=0.7,R=2000,B=2000,true_ratio=1/3):
    hit=0; widths=[]
    for _ in range(R):
        z=rng.standard_normal((n,2))
        a=z[:,0]; b=rho*z[:,0]+np.sqrt(1-rho**2)*z[:,1]
        # лог-стоимость суток: B0 и итог; s — sd лог суточной стоимости
        logA=np.log(6.0)+s*a
        logB=np.log(6.0*true_ratio)+s*b
        A=np.exp(logA); Bv=np.exp(logB)
        idx=rng.integers(0,n,(B,n))
        r=np.median(Bv[idx],axis=1)/np.median(A[idx],axis=1)
        lo,hi=np.percentile(r,[2.5,97.5])
        hit+= (lo<=true_ratio<=hi)
        widths.append(hi/lo)
    return hit/R, np.median(widths)
for s in (0.2,0.3,0.5):
    for n in (10,30):
        c,w=sim(n=n,s=s)
        print(f"s={s} n={n}: покрытие={c:.3f}, медианная ширина ДИ (hi/lo)={w:.2f}")
