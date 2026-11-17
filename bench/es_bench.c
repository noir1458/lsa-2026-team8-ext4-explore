#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <pthread.h>
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

#define EXT4_IOC_CLEAR_ES_CACHE _IO('f', 40)
struct worker { int fd; size_t n; off_t *offsets; uint64_t errors, digest; };
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cond = PTHREAD_COND_INITIALIZER;
static unsigned waiting, go;
static void fail(const char *what) { perror(what); exit(2); }
static uint64_t ns(void) { struct timespec t; if(clock_gettime(CLOCK_MONOTONIC,&t))fail("clock"); return (uint64_t)t.tv_sec*1000000000+t.tv_nsec; }
static uint64_t rng(uint64_t *s) { uint64_t x=(*s+=UINT64_C(0x9e3779b97f4a7c15)); x=(x^(x>>30))*UINT64_C(0xbf58476d1ce4e5b9);x=(x^(x>>27))*UINT64_C(0x94d049bb133111eb);return x^(x>>31); }
static uint64_t number(const char *s) { char *e;errno=0;uint64_t n=strtoull(s,&e,10);if(errno||!*s||*s=='-'||*e){fprintf(stderr,"invalid number\n");exit(2);}return n; }
static void send_byte(int fd) { char c='R';if(fd>=0){if(write(fd,&c,1)!=1)fail("pipe write");close(fd);} }
static void read_byte(int fd) { char c;if(fd>=0){if(read(fd,&c,1)!=1)fail("pipe read");close(fd);} }
static uint64_t check_seek(int fd, off_t offset, int whence, off_t expected) {
    errno=0;off_t got=lseek(fd,offset,whence);
    if(got==expected && (expected!=-1 || errno==ENXIO)) return 0;
    fprintf(stderr,"seek mismatch offset=%lld op=%d expected=%lld got=%lld errno=%d\n",(long long)offset,whence,(long long)expected,(long long)got,errno);return 1;
}
static uint64_t geometry(int fd, uint64_t extents, uint64_t span) {
    uint64_t errors=0;
    for(uint64_t i=0;i<extents;i++) {
        off_t data=(off_t)(i*2*span),hole=data+(off_t)span,next=data+(off_t)(2*span);
        errors+=check_seek(fd,data,SEEK_DATA,data);
        errors+=check_seek(fd,data,SEEK_HOLE,hole);
        errors+=check_seek(fd,hole,SEEK_HOLE,hole);
        errors+=check_seek(fd,hole,SEEK_DATA,i+1<extents?next:-1);
        if(errors>10) break;
    }
    return errors;
}
static void *run(void *arg) {
    struct worker *w=arg;
#ifdef __linux__
    if(prctl(PR_SET_NAME,"es_bench",0,0,0))fail("prctl");
#endif
    pthread_mutex_lock(&mutex);waiting++;pthread_cond_broadcast(&cond);
    while(!go)pthread_cond_wait(&cond,&mutex);
    pthread_mutex_unlock(&mutex);
    uint64_t digest=UINT64_C(14695981039346656037), errors=0;
    for(size_t i=0;i<w->n;i++) {
        off_t got=lseek(w->fd,w->offsets[i],SEEK_DATA);
        if(got!=w->offsets[i])errors++;
        digest=(digest^(uint64_t)got)*UINT64_C(1099511628211);
    }
    w->errors=errors;w->digest=digest;return NULL;
}
int main(int argc,char **argv) {
    const char *path=NULL,*pattern="hot",*cache="es-warm";
    uint64_t extents=1024,span=65536,queries=200000,seed=1,threads=1;
    int ready=-1,start=-1,done=-1,release=-1,verify_only=0;
    enum { F=1000,E,S,Q,P,C,SEED,T,R,START,DONE,RELEASE,V };
    struct option opts[]={{"file",1,0,F},{"extents",1,0,E},{"span",1,0,S},{"queries",1,0,Q},{"pattern",1,0,P},{"cache",1,0,C},{"seed",1,0,SEED},{"threads",1,0,T},{"ready-fd",1,0,R},{"start-fd",1,0,START},{"done-fd",1,0,DONE},{"release-fd",1,0,RELEASE},{"verify-only",0,0,V},{0,0,0,0}};
    int c;
    while((c=getopt_long(argc,argv,"",opts,NULL))!=-1)switch(c){
      case F:path=optarg;break;case E:extents=number(optarg);break;case S:span=number(optarg);break;
      case Q:queries=number(optarg);break;case P:pattern=optarg;break;case C:cache=optarg;break;
      case SEED:seed=number(optarg);break;case T:threads=number(optarg);break;
      case R:ready=(int)number(optarg);break;case START:start=(int)number(optarg);break;
      case DONE:done=(int)number(optarg);break;case RELEASE:release=(int)number(optarg);break;
      case V:verify_only=1;break;default:return 2;
    }
    if(!path||!extents||extents>1000000||span<4096||span%4096||span>16777216||!queries||queries>100000000||threads<1||threads>2||
       (strcmp(pattern,"hot")&&strcmp(pattern,"sequential")&&strcmp(pattern,"random"))||
       (strcmp(cache,"es-warm")&&strcmp(cache,"es-cold"))||
       ((ready<0)!=(start<0))||((done<0)!=(release<0))) {fprintf(stderr,"invalid arguments\n");return 2;}
    int fd=open(path,O_RDONLY);if(fd<0)fail("open");
    struct stat st;if(fstat(fd,&st))fail("fstat");
    if(st.st_size!=(off_t)(extents*2*span)){fprintf(stderr,"fixture size mismatch\n");return 2;}
    uint64_t errors=geometry(fd,extents,span);
    if(errors||verify_only){printf("{\"geometry_errors\":%"PRIu64"}\n",errors);return errors?1:0;}
    /* Explicit per-trial ES reset; metadata/page caches are not dropped. */
    if(ioctl(fd,EXT4_IOC_CLEAR_ES_CACHE))fail("EXT4_IOC_CLEAR_ES_CACHE (requires root/ext4)");
    if(!strcmp(cache,"es-warm")) for(uint64_t i=0;i<extents;i++)
        errors+=check_seek(fd,(off_t)(i*2*span),SEEK_DATA,(off_t)(i*2*span));
    if(errors)return 1;
    struct worker *workers=calloc(threads,sizeof(*workers));pthread_t tids[2];
    if(!workers)fail("calloc");
    for(uint64_t t=0;t<threads;t++) {
        struct worker *w=&workers[t];w->fd=open(path,O_RDONLY);if(w->fd<0)fail("thread open");
        w->n=(size_t)queries;w->offsets=calloc(w->n,sizeof(*w->offsets));if(!w->offsets)fail("queries calloc");
        uint64_t state=seed+t*UINT64_C(1000003),pages=span/4096;
        for(uint64_t i=0;i<queries;i++) {
            uint64_t extent,page;
            if(!strcmp(pattern,"hot")){extent=(extents/3+t)%extents;page=i%pages;}
            else if(!strcmp(pattern,"sequential")){extent=(i/pages+t)%extents;page=i%pages;}
            else {extent=rng(&state)%extents;page=rng(&state)%pages;}
            w->offsets[i]=(off_t)(extent*2*span+page*4096);
        }
        if(pthread_create(&tids[t],NULL,run,w))fail("pthread_create");
    }
    pthread_mutex_lock(&mutex);while(waiting<threads)pthread_cond_wait(&cond,&mutex);pthread_mutex_unlock(&mutex);
    (void)ns();send_byte(ready);read_byte(start);
    uint64_t begin=ns();pthread_mutex_lock(&mutex);go=1;pthread_cond_broadcast(&cond);pthread_mutex_unlock(&mutex);
    for(uint64_t t=0;t<threads;t++)if(pthread_join(tids[t],NULL))fail("pthread_join");
    uint64_t elapsed=ns()-begin;send_byte(done);read_byte(release);
    uint64_t digest=0;for(uint64_t t=0;t<threads;t++){errors+=workers[t].errors;digest^=workers[t].digest;close(workers[t].fd);free(workers[t].offsets);}free(workers);close(fd);
    printf("{\"pattern\":\"%s\",\"cache_state\":\"%s\",\"threads\":%"PRIu64",\"seed\":%"PRIu64",\"extents\":%"PRIu64",\"span\":%"PRIu64",\"queries\":%"PRIu64",\"elapsed_ns\":%"PRIu64",\"ns_per_query\":%.3f,\"errors\":%"PRIu64",\"digest\":\"%016"PRIx64"\"}\n",pattern,cache,threads,seed,extents,span,queries*threads,elapsed,(double)elapsed/(queries*threads),errors,digest);
    return errors?1:0;
}
