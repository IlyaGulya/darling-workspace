#include <malloc/malloc.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

/* Disposable public-API stress: readers retain ownership while a zone grows. */
enum { READERS = 8, BLOCKS = 262144, BLOCK_SIZE = 1008, ROUNDS = 8 };
static malloc_zone_t *zone;
static atomic_int ready;
static atomic_int stop;
static atomic_ulong checks;

static void *reader(void *argument)
{
    (void)argument;
    void *anchor = malloc_zone_malloc(zone, 272);
    if (!anchor) abort();
    atomic_fetch_add(&ready, 1);
    unsigned long count = 0;
    while (!atomic_load_explicit(&stop, memory_order_relaxed)) {
        if (malloc_size(anchor) != 272) {
            fprintf(stderr, "MALLOC_REGION_GROWTH_LOST_LIVE_BLOCK ptr=%p size=%zu\n", anchor, malloc_size(anchor));
            abort();
        }
        void *temporary = malloc_zone_malloc(zone, 272);
        if (!temporary) abort();
        free(temporary);
        ++count;
    }
    free(anchor);
    atomic_fetch_add(&checks, count);
    return NULL;
}

int main(void)
{
    void **blocks = calloc(BLOCKS, sizeof(*blocks));
    if (!blocks) return 2;
    for (int round = 0; round < ROUNDS; ++round) {
        zone = malloc_create_zone(0, 0);
        if (!zone) return 2;
        atomic_store(&ready, 0);
        atomic_store(&stop, 0);
        atomic_store(&checks, 0);
        pthread_t threads[READERS];
        for (int i = 0; i < READERS; ++i)
            if (pthread_create(&threads[i], NULL, reader, NULL)) abort();
        while (atomic_load(&ready) != READERS) sched_yield();
        for (size_t i = 0; i < BLOCKS; ++i) {
            blocks[i] = malloc_zone_malloc(zone, BLOCK_SIZE);
            if (!blocks[i]) abort();
            *(volatile unsigned char *)blocks[i] = (unsigned char)i;
        }
        atomic_store(&stop, 1);
        for (int i = 0; i < READERS; ++i)
            if (pthread_join(threads[i], NULL)) abort();
        for (size_t i = 0; i < BLOCKS; ++i) {
            if (*(unsigned char *)blocks[i] != (unsigned char)i || malloc_size(blocks[i]) != BLOCK_SIZE) abort();
            free(blocks[i]);
        }
        printf("MALLOC_REGION_GROWTH_ROUND round=%d checks=%lu\n", round, atomic_load(&checks));
        fflush(stdout);
        malloc_destroy_zone(zone);
    }
    free(blocks);
    puts("MALLOC_REGION_GROWTH_OK");
    return 0;
}
