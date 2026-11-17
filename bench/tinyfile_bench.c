#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>
#ifdef __linux__
#include <linux/fs.h>
#include <sys/ioctl.h>
#endif
#ifndef FS_INLINE_DATA_FL
#define FS_INLINE_DATA_FL 0x10000000
#endif

/* Existing ext4 feature screening. No kernel modification is assumed.
 * Buffer construction and exact validation are outside the timed interval.
 * Create/grow measure buffered file operations + fsync(directory) + syncfs.
 * Read measures open/read/close of each file (one pass); then validates bytes.
 */
struct io { uint64_t rchar,wchar,syscr,syscw,read_bytes,write_bytes,cancelled_write_bytes; int available; };
struct state { uint64_t errors,bytes,blocks512,zero_block_files,inline_files,flags_ok,flags_errors,digest; };
static uint64_t mix(uint64_t x) {
    x=(x^(x>>30))*UINT64_C(0xbf58476d1ce4e5b9);
    x=(x^(x>>27))*UINT64_C(0x94d049bb133111eb);
    return x^(x>>31);
}
static uint64_t next_random(uint64_t *s) { *s+=UINT64_C(0x9e3779b97f4a7c15);return mix(*s); }
static void die(const char *what) { perror(what);exit(2); }
static void fail(const char *what) { fprintf(stderr,"%s\n",what);exit(2); }
static uint64_t number(const char *s) {
    char *end;errno=0;if(!*s||*s=='-')fail("nonnegative integer required");
    unsigned long long n=strtoull(s,&end,10);if(errno||*end)fail("invalid integer");return n;
}
static uint64_t now_ns(void) { struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))die("clock");return (uint64_t)t.tv_sec*1000000000+(uint64_t)t.tv_nsec; }
static uint64_t tv_ns(struct timeval t) { return (uint64_t)t.tv_sec*1000000000+(uint64_t)t.tv_usec*1000; }
static void *alloc(size_t n,size_t z) { if(z&&n>SIZE_MAX/z)fail("allocation overflow");void *p=calloc(n,z);if(!p)die("calloc");return p; }
static void filename(char name[40],size_t id) { snprintf(name,40,"file_%08zu",id); }
static void content(unsigned char *p,size_t size,size_t id,uint64_t seed) {
    uint64_t state=mix(seed^((uint64_t)id*UINT64_C(0xd6e8feb86659fd93)));
    for(size_t off=0;off<size;) { uint64_t v=next_random(&state);for(unsigned j=0;j<8&&off<size;j++,off++)p[off]=(unsigned char)(v>>(j*8)); }
}
static void write_all(int fd,const unsigned char *p,size_t n,off_t offset) {
    size_t off=0;while(off<n) { ssize_t k=pwrite(fd,p+off,n-off,offset+(off_t)off);if(k<0&&errno==EINTR)continue;if(k<=0)die("pwrite");off+=(size_t)k; }
}
static void read_all(int fd,unsigned char *p,size_t n) {
    size_t off=0;while(off<n) { ssize_t k=read(fd,p+off,n-off);if(k<0&&errno==EINTR)continue;if(k<0)die("read");if(k==0)fail("short file");off+=(size_t)k; }
    unsigned char byte;ssize_t k;do{k=read(fd,&byte,1);}while(k<0&&errno==EINTR);if(k<0)die("read EOF");if(k!=0)fail("unexpected trailing data");
}
static struct io process_io(void) {
    struct io v={0};
#ifdef __linux__
    FILE *f=fopen("/proc/self/io","r");if(!f)die("/proc/self/io");char key[64];uint64_t n;
    while(fscanf(f,"%63s %"SCNu64,key,&n)==2) {
        if(!strcmp(key,"rchar:"))v.rchar=n;else if(!strcmp(key,"wchar:"))v.wchar=n;
        else if(!strcmp(key,"syscr:"))v.syscr=n;else if(!strcmp(key,"syscw:"))v.syscw=n;
        else if(!strcmp(key,"read_bytes:"))v.read_bytes=n;else if(!strcmp(key,"write_bytes:"))v.write_bytes=n;
        else if(!strcmp(key,"cancelled_write_bytes:"))v.cancelled_write_bytes=n;
    }
    if(ferror(f)||fclose(f))die("read process io");
    v.available=1;
#endif
    return v;
}
static void print_io(const struct io *v) {
    printf("{\"available\":%s,\"rchar\":%"PRIu64",\"wchar\":%"PRIu64",\"syscr\":%"PRIu64",\"syscw\":%"PRIu64",\"read_bytes\":%"PRIu64",\"write_bytes\":%"PRIu64",\"cancelled_write_bytes\":%"PRIu64"}",v->available?"true":"false",v->rchar,v->wchar,v->syscr,v->syscw,v->read_bytes,v->write_bytes,v->cancelled_write_bytes);
}
static void barrier(int ready,int start) {
    char byte='R';ssize_t n;
    if(ready>=0) { do{n=write(ready,&byte,1);}while(n<0&&errno==EINTR);if(n!=1)die("ready pipe");close(ready); }
    if(start>=0) { do{n=read(start,&byte,1);}while(n<0&&errno==EINTR);if(n!=1||byte!='S')fail("start pipe closed or invalid");close(start); }
}
static void sync_filesystem(int dirfd) {
    if(fsync(dirfd)) {
#ifndef __linux__
        if(errno!=EINVAL&&errno!=ENOTSUP)die("fsync directory");
#else
        die("fsync directory");
#endif
    }
#ifdef __linux__
    if(syncfs(dirfd))die("syncfs");
#endif
}
static void read_files(int dirfd,const size_t *order,size_t count,size_t size,unsigned char *data) {
    for(size_t i=0;i<count;i++) { size_t id=order[i];char name[40];filename(name,id);
        int fd=openat(dirfd,name,O_RDONLY|O_NOFOLLOW);if(fd<0)die("open read");
        read_all(fd,data+id*size,size);if(close(fd))die("close read");
    }
}
static struct state validate(int dirfd,size_t count,size_t size,const unsigned char *expected,const unsigned char *loaded,size_t stride) {
    struct state s={.digest=UINT64_C(14695981039346656037)};unsigned char *buf=alloc(size,1);
    for(size_t id=0;id<count;id++) { char name[40];filename(name,id);int fd=openat(dirfd,name,O_RDONLY|O_NOFOLLOW);if(fd<0)die("validate open");
        struct stat st;if(fstat(fd,&st))die("fstat");if(!S_ISREG(st.st_mode)||(uint64_t)st.st_size!=size)fail("invalid file type/size");
        const unsigned char *observed=loaded?loaded+id*size:buf;
        if(!loaded)read_all(fd,buf,size);
        if(memcmp(observed,expected+id*stride,size)) { if(s.errors<5)fprintf(stderr,"content mismatch: %s\n",name);s.errors++; }
        for(size_t k=0;k<size;k++)s.digest=(s.digest^observed[k])*UINT64_C(1099511628211);
        s.bytes+=(uint64_t)st.st_size;s.blocks512+=(uint64_t)st.st_blocks;if(!st.st_blocks)s.zero_block_files++;
#ifdef __linux__
        long flags=0;if(ioctl(fd,FS_IOC_GETFLAGS,&flags))s.flags_errors++;else{s.flags_ok++;if(flags&FS_INLINE_DATA_FL)s.inline_files++;}
#endif
        if(close(fd))die("validate close");
    }
    free(buf);return s;
}
static void print_state(struct state s) {
    printf("{\"validation_errors\":%"PRIu64",\"logical_bytes\":%"PRIu64",\"st_blocks_512_total\":%"PRIu64",\"zero_block_files\":%"PRIu64",\"inline_files\":%"PRIu64",\"flag_queries_ok\":%"PRIu64",\"flag_query_errors\":%"PRIu64",\"content_digest_fnv64\":\"%016"PRIx64"\"}",s.errors,s.bytes,s.blocks512,s.zero_block_files,s.inline_files,s.flags_ok,s.flags_errors,s.digest);
}
int main(int argc,char **argv) {
    const char *dir=NULL,*mode=NULL;size_t count=1000,size=32,initial=32;uint64_t seed=20261118,content_seed=20261118;int warmup=0,ready=-1,start=-1;
    static const struct option opts[]={{"dir",1,0,'d'},{"mode",1,0,'m'},{"count",1,0,'n'},{"size",1,0,'z'},{"initial-size",1,0,'i'},{"seed",1,0,'s'},{"content-seed",1,0,'c'},{"warmup",0,0,'w'},{"ready-fd",1,0,'r'},{"start-fd",1,0,'g'},{0,0,0,0}};
    int o;while((o=getopt_long(argc,argv,"",opts,NULL))!=-1)switch(o){case'd':dir=optarg;break;case'm':mode=optarg;break;case'n':count=number(optarg);break;case'z':size=number(optarg);break;case'i':initial=number(optarg);break;case's':seed=number(optarg);break;case'c':content_seed=number(optarg);break;case'w':warmup=1;break;case'r':ready=(int)number(optarg);break;case'g':start=(int)number(optarg);break;default:fail("invalid option");}
    if(!dir||!mode||optind!=argc||!count||count>10000000||!size||size>1048576)fail("required: --dir DIR --mode create|read|grow|verify --count N --size BYTES");
    int create=!strcmp(mode,"create"),read_mode=!strcmp(mode,"read"),grow=!strcmp(mode,"grow"),verify=!strcmp(mode,"verify");
    if(!create&&!read_mode&&!grow&&!verify)fail("unknown mode");
    if(grow&&(!initial||initial>=size))fail("grow requires initial-size < size");
    if((ready>=0)!=(start>=0))fail("both pipe descriptors are required");
    if(warmup&&!read_mode)fail("warmup is only for read mode");
    int dirfd=open(dir,O_RDONLY|O_DIRECTORY|O_NOFOLLOW);if(dirfd<0)die("open directory");
    unsigned char *expected=alloc(count,size),*loaded=read_mode?alloc(count,size):NULL;size_t *order=alloc(count,sizeof(*order));
    for(size_t i=0;i<count;i++){order[i]=i;content(expected+i*size,size,i,content_seed);}uint64_t random_state=seed;
    for(size_t i=count;i>1;i--){size_t j=(size_t)(next_random(&random_state)%i),v=order[i-1];order[i-1]=order[j];order[j]=v;}
    uint64_t order_digest=UINT64_C(14695981039346656037);for(size_t i=0;i<count;i++)order_digest=(order_digest^order[i])*UINT64_C(1099511628211);
    struct state before={0};if(grow){before=validate(dirfd,count,initial,expected,NULL,size);if(before.errors)fail("grow source validation failed");}
    if(warmup){read_files(dirfd,order,count,size,loaded);before=validate(dirfd,count,size,expected,loaded,size);if(before.errors)fail("warmup validation failed");}
    if(verify){struct state v=validate(dirfd,count,size,expected,NULL,size);printf("{\"schema\":1,\"mode\":\"verify\",\"count\":%zu,\"size\":%zu,\"state\":",count,size);print_state(v);puts("}");close(dirfd);free(expected);free(loaded);free(order);return v.errors?1:0;}
    barrier(ready,start);struct io io_before=process_io();struct rusage ru_before,ru_after;if(getrusage(RUSAGE_SELF,&ru_before))die("getrusage");
    uint64_t begin=now_ns();
    if(read_mode)read_files(dirfd,order,count,size,loaded);
    else for(size_t i=0;i<count;i++) { size_t id=order[i];char name[40];filename(name,id);
        int fd=openat(dirfd,name,O_WRONLY|O_NOFOLLOW|(create?(O_CREAT|O_EXCL):0),0644);if(fd<0)die("open write");
        size_t off=grow?initial:0;write_all(fd,expected+id*size+off,size-off,(off_t)off);if(close(fd))die("close write");
    }
    uint64_t operations_end=now_ns();
    if(!read_mode)sync_filesystem(dirfd);
    uint64_t end=read_mode?operations_end:now_ns();
    if(getrusage(RUSAGE_SELF,&ru_after))die("getrusage");
    struct io io_after=process_io();
    struct state after=validate(dirfd,count,size,expected,loaded,size);struct utsname u;if(uname(&u))die("uname");
    printf("{\"schema\":1,\"mode\":\"%s\",\"count\":%zu,\"size\":%zu,\"initial_size\":%zu,\"seed\":%"PRIu64",\"content_seed\":%"PRIu64",\"warmup\":%s,\"kernel\":\"%s\",\"order_digest_fnv64\":\"%016"PRIx64"\",\"operations_ns\":%"PRIu64",\"sync_ns\":%"PRIu64",\"elapsed_ns\":%"PRIu64",\"ns_per_file\":%.6f,",mode,count,size,grow?initial:0,seed,content_seed,warmup?"true":"false",u.release,order_digest,operations_end-begin,end-operations_end,end-begin,(double)(end-begin)/count);
#ifdef __linux__
    printf("\"syncfs_supported\":true,");
#else
    printf("\"syncfs_supported\":false,");
#endif
    printf("\"rusage_delta\":{\"user_ns\":%"PRIu64",\"system_ns\":%"PRIu64",\"minor_faults\":%ld,\"major_faults\":%ld,\"inblock\":%ld,\"oublock\":%ld},\"process_io_before\":",tv_ns(ru_after.ru_utime)-tv_ns(ru_before.ru_utime),tv_ns(ru_after.ru_stime)-tv_ns(ru_before.ru_stime),ru_after.ru_minflt-ru_before.ru_minflt,ru_after.ru_majflt-ru_before.ru_majflt,ru_after.ru_inblock-ru_before.ru_inblock,ru_after.ru_oublock-ru_before.ru_oublock);
    print_io(&io_before);printf(",\"process_io_after\":");print_io(&io_after);printf(",\"before_state\":");print_state(before);printf(",\"state\":");print_state(after);puts("}");
    if(close(dirfd))die("close directory");
    free(expected);free(loaded);free(order);return after.errors?1:0;
}
