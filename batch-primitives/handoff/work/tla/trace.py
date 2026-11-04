import re,sys
txt=open(sys.argv[1]).read().replace('\r','')
src=open('BatchLease.tla').read().split('\n')
blocks=re.split(r'\n(?=State \d+:)',txt)
prev={}
for b in blocks:
    m=re.match(r'State (\d+): <(\S+)?.*?line (\d+)',b)
    if not m:
        m0=re.match(r'State (\d+): <Initial',b)
        if not m0: continue
    body=b.split('\n',1)[1] if '\n' in b else ''
    vs={}
    for part in re.split(r'\n(?=/\\ )',body):
        part=part.strip()
        if not part.startswith('/\\ '): continue
        k,_,v=part[3:].partition(' = ')
        vs[k]=re.sub(r'\s+',' ',v).split('\n\n')[0]
    hdr=b.split('\n',1)[0]
    act=''
    mm=re.search(r'<(\w+)',hdr); act=mm.group(1) if mm else ''
    ch={k:v for k,v in vs.items() if prev.get(k)!=v and k not in('nextMsg','wId','nextRv','sentAt','wBy')}
    num=re.match(r'State (\d+)',hdr).group(1)
    print(f"{num:>3} {act}: "+'; '.join(f"{k}={v}" for k,v in ch.items()))
    prev=vs
