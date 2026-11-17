#!/usr/bin/env python3
"""Compile the actual tail helper and check the full-match wrapper under ASan/UBSan."""
from pathlib import Path
import re,subprocess,tempfile
r=Path(__file__).resolve().parents[1]
s=(r/'source/namei-leaf.c').read_text()
helper=re.search(r'static bool leaf_tail_equal\([^}]+\}',s,re.S).group(0)
test=r'''
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
HELPER
static bool match(const unsigned char*a,unsigned al,const unsigned char*b,unsigned bl,int mode){
 if(mode && al==bl && al>=8 && !leaf_tail_equal(a,b,al)) return false;
 return al==bl && !memcmp(a,b,al);
}
int main(void){
 unsigned long checks=0;
 for(unsigned n=1;n<=255;n++){
  unsigned char *a=malloc(n),*b=malloc(n);
  for(unsigned i=0;i<n;i++) a[i]=b[i]=(i*17+n)%255+1;
  if(!match(a,n,b,n,1)) abort(); checks++;
  for(unsigned j=0;j<n;j++){
   unsigned char old=b[j]; b[j]^=127;
   if(match(a,n,b,n,0)!=match(a,n,b,n,1)) abort(); checks++;b[j]=old;
  }
  for(unsigned m=1;m<=n;m++){
   if(match(a,n,b,m,0)!=match(a,n,b,m,1)) abort();checks++;
  }
  free(a);free(b);
 }
 printf("{\"checks\":%lu,\"errors\":0}\n",checks); return 0;
}
'''.replace('HELPER',helper)
with tempfile.TemporaryDirectory() as d:
 p=Path(d);(p/'check.c').write_text(test)
 subprocess.run(['cc','-O2','-Wall','-Wextra','-fsanitize=address,undefined',str(p/'check.c'),'-o',str(p/'check')],check=True)
 subprocess.run([str(p/'check')],check=True)
