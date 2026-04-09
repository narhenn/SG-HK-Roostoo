"""
Backtest: FIXED vs OLD naked trader
FIXED: score>=6, chart gate, top 25 coins, 24h loss cooldown, 10 patterns
OLD: score>=4, all coins, 1h cooldown, 50 patterns (what lost $7,384 live)

Run: python3 backtest_fixed_vs_old.py
Takes ~10 min (chart pattern detection is heavy)
"""
import json,numpy as np,pandas as pd,sys,random,time as tm
sys.path.insert(0,'/Users/narhen/SG-HK-Roostoo')
from pattern_encyclopedia import ChartPatternDetector
FEE=0.001
TOP_COINS={'BTC','ETH','SOL','BNB','XRP','AVAX','LINK','FET','TAO','APT','SUI','NEAR',
           'PENDLE','ADA','DOT','UNI','HBAR','AAVE','CAKE','DOGE','FIL','LTC','SEI','ARB','ENA'}
SIZE_L=200000;SIZE_M=250000;SIZE_H=350000

def bdf(cs,tf):
    b=[]
    for i in range(0,len(cs)-tf+1,tf):
        ch=cs[i:i+tf]
        b.append({'open':ch[0]['o'],'high':max(x['h'] for x in ch),'low':min(x['l'] for x in ch),'close':ch[-1]['c'],'volume':sum(x['v'] for x in ch)})
    return pd.DataFrame(b)
def _bs(c):return abs(c['c']-c['o'])
def _gr(c):return c['c']>c['o']
def _rd(c):return c['c']<c['o']
def _rng(c):return c['h']-c['l']
def _uw(c):return c['h']-max(c['o'],c['c'])
def _lw(c):return min(c['o'],c['c'])-c['l']

def scan10(cl):
    n=len(cl)
    if n<5:return 0
    c=cl[-1];p=cl[-2] if n>=2 else c
    ab=sum(_bs(x) for x in cl[-14:])/min(14,n);ar=sum(_rng(x) for x in cl[-14:])/min(14,n)
    if ab==0:ab=.0001
    if ar==0:ar=.0001
    bs=_bs(c);rng=_rng(c);bsp=_bs(p);s=0
    if _rd(p) and _gr(c) and c['o']<=p['c'] and c['c']>=p['o'] and bs>bsp*1.2:s+=3
    if rng>0 and bs>0 and _lw(c)>=bs*2 and _uw(c)<=bs*0.5 and _gr(c):s+=3
    if n>=3 and all(_gr(cl[-i]) for i in [1,2,3]) and cl[-1]['c']>cl[-2]['c']>cl[-3]['c']:s+=3
    if n>=3 and cl[-2]['h']<=cl[-3]['h'] and cl[-2]['l']>=cl[-3]['l'] and cl[-1]['c']>cl[-3]['h'] and _gr(cl[-1]):s+=3
    if n>=5 and cl[-2]['l']>cl[-4]['l'] and cl[-1]['h']>cl[-3]['h'] and _gr(cl[-1]):s+=2
    if _gr(c) and bs>ab*2 and _uw(c)<bs*.1 and _lw(c)<bs*.1:s+=3
    if n>=3:
        b1,b2,b3=_bs(cl[-3]),_bs(cl[-2]),_bs(cl[-1])
        if _rd(cl[-3]) and b1>ab and b2<b1*.3 and _gr(cl[-1]) and b3>ab and cl[-1]['c']>(cl[-3]['o']+cl[-3]['c'])/2:s+=4
    if _rd(p) and _gr(c):
        mid=(p['o']+p['c'])/2
        if c['o']<p['c'] and c['c']>mid and c['c']<p['o']:s+=3
    if n>=2 and ar>0 and abs(c['l']-p['l'])/ar<.05 and _rd(p) and _gr(c):s+=3
    if n>=6 and (cl[-1]['c']-cl[-6]['c'])/cl[-6]['c']*100>=1.0:s+=2
    return s

chart_cache={}
def scan_chart(df,idx,coin):
    key=(coin,idx//2)
    if key in chart_cache:return chart_cache[key]
    if idx<20:chart_cache[key]=None;return None
    w=df.iloc[max(0,idx-79):idx+1].copy().reset_index(drop=True)
    if len(w)<20:chart_cache[key]=None;return None
    cols={'o':'open','h':'high','l':'low','c':'close','v':'volume'}
    for k,v in cols.items():
        if k in w.columns:w=w.rename(columns={k:v})
    if 'volume' not in w.columns:w['volume']=1.0
    try:det=ChartPatternDetector(w)
    except:chart_cache[key]=None;return None
    best=None
    for mn,ms2 in [('detect_bull_flag',5),('detect_ascending_triangle',5),('detect_double_bottom',4),
        ('detect_inverse_head_shoulders',5),('detect_falling_wedge',4),('detect_cup_and_handle',5),
        ('detect_symmetrical_triangle',4),('detect_rectangle_channel',4),('detect_rounding_bottom',4),
        ('detect_pennant',4),('detect_triple_bottom',5),('detect_v_bottom',3),('detect_measured_move',4)]:
        try:
            r2=getattr(det,mn)()
            if not r2:continue
            items=r2 if isinstance(r2,list) else [r2]
            for r in items:
                if not isinstance(r,dict):continue
                if 'bear' in r.get('pattern','').lower() or 'top' in r.get('pattern','').lower():continue
                e=r.get('entry',0);st=r.get('stop',0);tg=r.get('target',0)
                if e>0 and st>0 and tg>0 and tg>e and st<e:
                    rk=(e-st)/e*100;rw=(tg-e)/e*100;rr=rw/rk if rk>0 else 0
                    if rk<=4 and rr>=1.5:
                        if not best or rr>best[5]:best=(mn.replace('detect_','').upper(),ms2,e,st,tg,rr)
        except:pass
    chart_cache[key]=best
    return best

def run(cd,btc_c,version):
    global chart_cache;chart_cache={}
    tr=[];pos={};consec=0;gcd=0;coin_loss_cd={}
    mb=max(len(df) for df in cd.values())
    if version=='fixed':
        min_score=6;use_chart_gate=True;coin_filter=TOP_COINS;loss_cd=24
    else:
        min_score=4;use_chart_gate=False;coin_filter=None;loss_cd=1
    for i in range(15,mb):
        reg='BULL'
        if btc_c is not None and i<len(btc_c) and i>=20:
            em=np.mean(list(btc_c[max(0,i-20):i+1]))
            closes=list(btc_c[max(0,i-14):i+1])
            d2=[closes[j]-closes[j-1] for j in range(1,len(closes))]
            g=[d for d in d2 if d>0];l=[-d for d in d2 if d<0]
            ag=sum(g)/14 if g else .001;al=sum(l)/14 if l else .001
            rsi=100-100/(1+ag/al)
            c3=(btc_c[i]-btc_c[max(0,i-3)])/btc_c[max(0,i-3)]*100
            if btc_c[i]<em and (rsi<40 or c3<-0.5):reg='BEAR'
        if reg!='BULL':
            for co in list(pos.keys()):
                df=cd[co]
                if i>=len(df):continue
                p=pos[co];ca=df['close'].values;ha=df['high'].values;la=df['low'].values
                a14=pd.Series(ha-la).rolling(14).mean().values
                hd=i-p['i'];pp=(ca[i]-p['e'])/p['e']
                if ha[i]>p['pk']:p['pk']=ha[i]
                sell=False;ep=ca[i]
                if hd<3:
                    hrd=p['e']-a14[i]*2.0 if i<len(a14) and a14[i]>0 else p['e']*.97
                    if la[i]<=hrd:sell=True;ep=hrd
                    else:continue
                if not sell and p.get('ct',0)>0 and ha[i]>=p['ct']:sell=True;ep=p['ct']
                if not sell:
                    sl=p.get('cs',0)
                    if sl<=0:sl=p['e']-a14[i]*1.0 if i<len(a14) and a14[i]>0 else p['e']*.993
                    if la[i]<=sl:sell=True;ep=sl
                if not sell and pp>0.01 and not p.get('pt'):
                    tr.append({'c':co,'pnl':(ca[i]-p['e'])*p['q']*.5-p['sz']*.5*FEE*2,'r':'P'})
                    p['q']*=.5;p['pt']=True
                if not sell and pp>0.003:
                    ns=p['pk']*(1-0.01)
                    if ns>p.get('s',0):p['s']=ns
                if not sell and p.get('s',0)>p['e'] and ca[i]<=p['s']:sell=True;ep=p['s']
                if not sell and hd>=12:sell=True
                if sell:
                    mt=.5 if p.get('pt') else 1
                    pnl=(ep-p['e'])*p['q']-p['sz']*mt*FEE*2
                    tr.append({'c':co,'pnl':pnl,'r':'EX'})
                    if pnl<0:coin_loss_cd[co]=i+loss_cd
                    del pos[co]
            continue
        for co in list(pos.keys()):
            df=cd[co]
            if i>=len(df):continue
            p=pos[co];ca=df['close'].values;ha=df['high'].values;la=df['low'].values
            a14=pd.Series(ha-la).rolling(14).mean().values
            hd=i-p['i'];pp=(ca[i]-p['e'])/p['e']
            if ha[i]>p['pk']:p['pk']=ha[i]
            sell=False;ep=ca[i]
            if hd<3:
                hrd=p['e']-a14[i]*2.0 if i<len(a14) and a14[i]>0 else p['e']*.97
                if la[i]<=hrd:sell=True;ep=hrd
                else:continue
            if not sell and p.get('ct',0)>0 and ha[i]>=p['ct']:sell=True;ep=p['ct']
            if not sell:
                sl=p.get('cs',0)
                if sl<=0:sl=p['e']-a14[i]*1.2 if i<len(a14) and a14[i]>0 else p['e']*.993
                if la[i]<=sl:sell=True;ep=sl
            if not sell and pp>0.01 and not p.get('pt'):
                tr.append({'c':co,'pnl':(ca[i]-p['e'])*p['q']*.5-p['sz']*.5*FEE*2,'r':'P'})
                p['q']*=.5;p['pt']=True
            if not sell and pp>0.003:
                ns=p['pk']*(1-0.01)
                if ns>p.get('s',0):p['s']=ns
            if not sell and p.get('s',0)>p['e'] and ca[i]<=p['s']:sell=True;ep=p['s']
            if not sell and hd>=12:sell=True
            if sell:
                mt=.5 if p.get('pt') else 1
                pnl=(ep-p['e'])*p['q']-p['sz']*mt*FEE*2
                tr.append({'c':co,'pnl':pnl,'r':'EX'})
                del pos[co]
                if pnl<0:consec+=1;coin_loss_cd[co]=i+loss_cd
                else:consec=0
                if consec>=3:gcd=i+3
        if i<gcd or len(pos)>=4:continue
        best=None
        for co,df in cd.items():
            if coin_filter and co not in coin_filter:continue
            if co in pos or i>=len(df):continue
            if co in coin_loss_cd and i<coin_loss_cd[co]:continue
            ca=df['close'].values;ha=df['high'].values;la=df['low'].values;va=df['volume'].values;oa=df['open'].values
            cl=[{'o':oa[j],'h':ha[j],'l':la[j],'c':ca[j],'v':va[j]} for j in range(max(0,i-25),i+1)]
            csc=scan10(cl)
            chart=scan_chart(df,i,co)
            chs=chart[1] if chart else 0
            total=csc+chs
            if use_chart_gate and not chart and csc<8:continue
            if total<min_score:continue
            a14=pd.Series(ha-la).rolling(14).mean().values
            if i<len(a14) and a14[i]>0 and a14[i]/ca[i]*100<0.3:continue
            if i>=10:
                t2=(ca[i]-ca[i-10])/ca[i-10]*100
                if t2>10.0 or t2<-10.0:continue
            cr2=ha[i]-la[i];cb2=abs(ca[i]-oa[i])
            if cr2>0 and cb2/cr2<0.1:continue
            if total>=10:sz=SIZE_H
            elif total>=8:sz=SIZE_M
            else:sz=SIZE_L
            prio=total*10+(5 if chart else 0)
            if not best or prio>best[0]:best=(prio,co,ca[i],sz,chart)
        if best:
            _,co,px,sz,chart=best
            e=px*1.0003
            ct=chart[4] if chart else 0;cs2=chart[3] if chart else 0
            pos[co]={'e':e,'q':sz/e,'pk':e,'i':i,'s':0,'sz':sz,'ct':ct,'cs':cs2}
    return tr

t0=tm.time()
with open('data/binance_1m_7d.json') as f:r1=json.load(f)
with open('data/1min_7days.json') as f:r2=json.load(f)
cn=list(r1.keys())
d11={c:bdf(v,60) for c,v in r1.items()};d21={c:bdf(v,60) for c,v in r2.items()}
d1f={c:bdf(v[:len(v)//2],60) for c,v in r1.items()};d1l={c:bdf(v[len(v)//2:],60) for c,v in r1.items()}
btc1=d11['BTC']['close'].values;btc2=d21['BTC']['close'].values
btc1f=d1f['BTC']['close'].values;btc1l=d1l['BTC']['close'].values

def rp(lb,tr):
    if not tr:print(f'  {lb:35s}:   0');return 0
    t=sum(x['pnl'] for x in tr);w=[x for x in tr if x['pnl']>0];wr=len(w)/len(tr)*100
    gp=sum(x['pnl'] for x in w) if w else 0;gl=abs(sum(x['pnl'] for x in tr if x['pnl']<=0)) or 1
    tg='***' if t>30000 else ('**' if t>10000 else ('*' if t>0 else 'X'))
    print(f'  {lb:35s}: {len(tr):3d}tr {wr:5.1f}%WR ${t:+10,.0f} PF={gp/gl:.2f} {tg}',flush=True)
    return t

print('FIXED vs OLD comparison')
print('Fixed: score>=6, chart gate, top 25 coins, 24h loss cooldown')
print('Old: score>=4, all coins, 1h cooldown')
print('='*70)

for ver in ['fixed','old']:
    print(f'\n{ver.upper()}:')
    r=[]
    for lb,d,b in [('D1',d11,btc1),('D1a',d1f,btc1f),('D1b',d1l,btc1l),('D2',d21,btc2)]:
        chart_cache={};r.append(rp(lb,run(d,b,ver)))
        print(f'    ({tm.time()-t0:.0f}s)',flush=True)
    print(f'  AVG: ${np.mean(r):+,.0f}')
    random.seed(42);cv=[]
    for rd in range(5):
        random.shuffle(cn);tc=set(cn[len(cn)//2:])
        d2x={c2:bdf(r1[c2],60) for c2 in tc};bx=d2x['BTC']['close'].values if 'BTC' in d2x else btc1
        chart_cache={};cv.append(sum(x['pnl'] for x in run(d2x,bx,ver)))
    print(f'  CV: all+={all(x>0 for x in cv)} avg=${np.mean(cv):+,.0f}')

print(f'\nTotal: {tm.time()-t0:.0f}s')
