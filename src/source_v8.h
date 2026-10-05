#ifndef SOURCE_V8_H
#define SOURCE_V8_H
#ifdef __cplusplus
extern "C" {
#endif
void *sv8_create(int timeout_ms, int heap_mb);
void sv8_start(void *runtime, const char *script, const char *variables,
               const char *prelude);
char *sv8_poll(void *runtime);
void sv8_resolve(void *runtime, int id, const char *value, int rejected);
char *sv8_sync_poll(void *runtime);
void sv8_sync_reply(void *runtime, int id, const char *value, int rejected);
void sv8_cancel(void *runtime);
void sv8_destroy(void *runtime);
void sv8_free(char *value);
const char *sv8_version(void);
#ifdef __cplusplus
}
#endif
#endif
