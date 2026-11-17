#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#ifdef __linux__
#include <sys/prctl.h>
#endif
#include <time.h>
#include <unistd.h>
#ifndef O_DIRECT
#define O_DIRECT 0
#endif
#define PAGE 4096
#define ES_CLEAR _IO('f',40)
static int active;
static void setting(const char *name,int value) {
    char path[256];snprintf(path,sizeof path,"/sys/module/ext4/parameters/%s",name);
    FILE *f=fopen(path,"w");if(!f){perror(path);exit(2);}if(fprintf(f,"%d\n",value)<0||fclose(f)){perror("parameter write");exit(2);}
}
static void restore(void) { if(active){active=0;setting("es_lookup_profile",0);setting("es_lookup_refresh",0);} }
static void fail(const char *s){perror(s);exit(2);}
static uint64_t number(const char *s){char *end;errno=0;uint64_t x=strtoull(s,&end,10);if(errno||!*s||*s=='-'||*end){fprintf(stderr,"invalid number\n");exit(2);}return x;}
static uint64_t ns(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))fail("clock");return (uint64_t)t.tv_sec*1000000000+t.tv_nsec;}
static uint64_t random64(uint64_t *s){uint64_t x=(*s+=UINT64_C(0x9e3779b97f4a7c15));x=(x^(x>>30))*UINT64_C(0xbf58476d1ce4e5b9);x=(x^(x>>27))*UINT64_C(0x94d049bb133111eb);return x^(x>>31);}
static int actual(const char *name){char path[256],c=0;snprintf(path,sizeof path,"/sys/module/ext4/parameters/%s",name);FILE*f=fopen(path,"r");if(!f)fail("read parameter");if(fread(&c,1,1,f)!=1)fail("parameter empty");fclose(f);return c=='Y'||c=='y'||c=='1';}
int main(int argc,char **argv){
    const char *path=NULL,*pattern="hot",*io="buffered";uint64_t extents=1024,span=65536,queries=4000,seed=1;int policy=0,profile=0;
    enum{F=1000,E,S,Q,SEED,P,I,MODE,PROFILE};
    struct option options[]={{"file",1,0,F},{"extents",1,0,E},{"span",1,0,S},{"queries",1,0,Q},{"seed",1,0,SEED},{"pattern",1,0,P},{"io",1,0,I},{"policy",1,0,MODE},{"profile",1,0,PROFILE},{0,0,0,0}};
    int c;while((c=getopt_long(argc,argv,"",options,NULL))!=-1)switch(c){case F:path=optarg;break;case E:extents=number(optarg);break;case S:span=number(optarg);break;case Q:queries=number(optarg);break;case SEED:seed=number(optarg);break;case P:pattern=optarg;break;case I:io=optarg;break;case MODE:policy=(int)number(optarg);break;case PROFILE:profile=(int)number(optarg);break;default:return 2;}
    if(!path||extents<2||extents>1000000||span<PAGE||span%PAGE||span>16777216||!queries||queries>1000000||policy<0||policy>1||profile<0||profile>1||
       (strcmp(pattern,"hot")&&strcmp(pattern,"random"))||(strcmp(io,"buffered")&&strcmp(io,"direct"))){fprintf(stderr,"invalid arguments\n");return 2;}
#ifdef __linux__
    if(prctl(PR_SET_NAME,"es_bench",0,0,0))fail("prctl");
#else
    fprintf(stderr,"Linux ext4 is required\n");return 2;
#endif
    atexit(restore);active=1;setting("es_lookup_profile",0);setting("es_lookup_refresh",0);
    int fd=open(path,O_RDONLY);if(fd<0)fail("open fixture");struct stat st;if(fstat(fd,&st))fail("fstat");if(st.st_size!=(off_t)(extents*2*span)){fprintf(stderr,"fixture size mismatch\n");return 2;}
    off_t *offsets=calloc((size_t)queries,sizeof(*offsets));ssize_t *returns=calloc((size_t)queries,sizeof(*returns));void *memory=NULL;
    if(!offsets||!returns||posix_memalign(&memory,PAGE,(size_t)queries*PAGE))fail("allocate aligned buffers");
    unsigned char *buffers=memory;memset(buffers,0,(size_t)queries*PAGE);
    uint64_t state=seed,query_digest=UINT64_C(14695981039346656037);
    for(uint64_t i=0;i<queries;i++){uint64_t extent,page;if(!strcmp(pattern,"hot")){extent=extents/3;page=i%(span/PAGE);}else{extent=random64(&state)%extents;page=random64(&state)%(span/PAGE);}offsets[i]=(off_t)(extent*2*span+page*PAGE);query_digest=(query_digest^(uint64_t)offsets[i])*UINT64_C(1099511628211);}
    /* Both modes receive identical ordinary-read warming before ES reset. */
    for(uint64_t i=0;i<queries;i++)if(pread(fd,buffers+i*PAGE,PAGE,offsets[i])!=PAGE)fail("warm pread");
    int measured_fd=fd;if(!strcmp(io,"direct")){measured_fd=open(path,O_RDONLY|O_DIRECT);if(measured_fd<0)fail("open O_DIRECT");}
    if(ioctl(fd,ES_CLEAR))fail("EXT4_IOC_CLEAR_ES_CACHE");
    for(uint64_t i=0;i<extents;i++){off_t offset=(off_t)(i*2*span);if(lseek(fd,offset,SEEK_DATA)!=offset)fail("ES warm SEEK_DATA");}
    /* Erase warmup bytes so a short/failed read cannot pass verification. */
    memset(buffers,0,(size_t)queries*PAGE);(void)ns();
    setting("es_lookup_refresh",policy);setting("es_lookup_reset",1);setting("es_lookup_profile",profile);
    int actual_policy=actual("es_lookup_refresh"),actual_profile=actual("es_lookup_profile");
    if(actual_policy!=policy||actual_profile!=profile){fprintf(stderr,"parameter mismatch\n");return 2;}
    uint64_t begin=ns();
    for(uint64_t i=0;i<queries;i++)returns[i]=pread(measured_fd,buffers+i*PAGE,PAGE,offsets[i]);
    uint64_t elapsed=ns()-begin;
    setting("es_lookup_profile",0);
    unsigned long long lookups,hits,nodes,treehits,misses,refreshes;
    FILE *stats=fopen("/sys/module/ext4/parameters/es_lookup_stats","r");if(!stats)fail("stats");
    if(fscanf(stats,"lookups=%llu pointer_hits=%llu tree_nodes=%llu tree_hits=%llu tree_misses=%llu refreshes=%llu",&lookups,&hits,&nodes,&treehits,&misses,&refreshes)!=6){fprintf(stderr,"bad stats\n");return 2;}fclose(stats);
    uint64_t errors=0;for(uint64_t i=0;i<queries;i++){if(returns[i]!=PAGE){errors++;continue;}for(size_t j=0;j<PAGE;j++)if(buffers[i*PAGE+j]!=(unsigned char)((j%251)+1)){errors++;break;}}
    if(!profile&&(lookups||hits||nodes||treehits||misses||refreshes)){fprintf(stderr,"counters nonzero during timing\n");return 2;}
    printf("{\"pattern\":\"%s\",\"io\":\"%s\",\"policy\":%d,\"profile\":%d,\"actual_policy\":%d,\"actual_profile\":%d,\"queries\":%"PRIu64",\"bytes\":%"PRIu64",\"seed\":%"PRIu64",\"elapsed_ns\":%"PRIu64",\"ns_per_query\":%.3f,\"errors\":%"PRIu64",\"query_digest\":\"%016"PRIx64"\",\"lookups\":%llu,\"pointer_hits\":%llu,\"tree_nodes\":%llu,\"tree_hits\":%llu,\"tree_misses\":%llu,\"refreshes\":%llu}\n",pattern,io,policy,profile,actual_policy,actual_profile,queries,queries*PAGE,seed,elapsed,(double)elapsed/queries,errors,query_digest,lookups,hits,nodes,treehits,misses,refreshes);
    if(measured_fd!=fd)close(measured_fd);
    close(fd);free(offsets);free(returns);free(buffers);restore();return errors?1:0;
}
