CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -Werror
all: lookup_bench es_bench tinyfile_bench es_io_bench
lookup_bench: lookup_bench.c
	$(CC) $(CFLAGS) $< -o $@
es_bench: es_bench.c
	$(CC) $(CFLAGS) -pthread $< -o $@
tinyfile_bench: tinyfile_bench.c
	$(CC) $(CFLAGS) $< -o $@
es_io_bench: es_io_bench.c
	$(CC) $(CFLAGS) $< -o $@
clean:
	rm -f lookup_bench es_bench tinyfile_bench es_io_bench
.PHONY: all clean
