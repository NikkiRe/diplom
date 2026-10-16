# H5(а): "тренд незначим (МК/Хамед–Рао p>0.05)" при выборке 30 с за 8 ч (n=960).
# Модель PSS Go-процесса: 20 МиБ + пила GC (период 120 с, амплитуда 2 МиБ) + AR(1)-шум + медленный рост slope МиБ/ч
import numpy as np
from math import erf, sqrt
rng=np.random.default_rng(3)
def mk_hr(x):
    n=len(x); t=np.arange(n)
    s=0
    for k in range(n-1): s+=np.sign(x[k+1:]-x[k]).sum()
    var=n*(n-1)*(2*n+5)/18
    # Hamed–Rao: автокорреляции рангов детрендированного ряда (наклон Сена)
    idx=np.triu_indices(n,1)
    slopes=(x[idx[1]]-x[idx[0]])/(idx[1]-idx[0]); b=np.median(slopes)
    r=x-b*t; rk=np.argsort(np.argsort(r)).astype(float); rk-=rk.mean()
    acf=[np.dot(rk[:-L],rk[L:])/np.dot(rk,rk) for L in range(1,n-1)]
    corr=0.0; lim=1.96/np.sqrt(n)
    for L,rho in enumerate(acf,1):
        if abs(rho)>lim: corr+=(n-L)*(n-L-1)*(n-L-2)*rho
    var*= 1+2/(n*(n-1)*(n-2))*corr
    z=(s-np.sign(s))/np.sqrt(var)
    p=2*(1-0.5*(1+erf(abs(z)/sqrt(2))))
    return p,b
n=960; dt=30/3600
for slope in (0.0,0.05,0.2,0.5):
    ps=[];bs=[]
    for rep in range(40):
        t=np.arange(n)*dt
        saw=2*((np.arange(n)*30)%120)/120
        e=np.zeros(n)
        for i in range(1,n): e[i]=0.6*e[i-1]+rng.normal(0,0.3)
        x=20+saw+e+slope*t
        p,b=mk_hr(x); ps.append(p); bs.append(b/dt)
    ps=np.array(ps)
    print(f"истинный наклон {slope} МиБ/ч: доля прогонов с p<0.05 = {np.mean(ps<0.05):.2f}; медиана оценки Сена {np.median(bs):.3f} МиБ/ч")
