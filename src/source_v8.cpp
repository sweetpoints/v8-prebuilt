#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <libplatform/libplatform.h>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <v8.h>
#include <vector>
using namespace v8;
// The external pointer table validates this tag when Runtime* is retrieved.
static constexpr ExternalPointerTypeTag kRuntimePointerTag = 1;
struct Runtime {
  Isolate *isolate;
  ArrayBuffer::Allocator *allocator;
  Global<Context> context;
  Global<Promise> result;
  std::map<int, Global<Promise::Resolver>> pending;
  std::vector<std::string> requests;
  std::vector<std::string> sync_requests;
  std::map<int, std::pair<std::string, bool>> sync_responses;
  int next = 0;
  std::mutex mutex;
  std::condition_variable cv;
  bool done = false;
  std::atomic<bool> expired{false};
  std::thread timer;
  std::string error;
};
static std::once_flag initialized;
static std::unique_ptr<Platform> runtime_platform;
static Local<String> str(Isolate *i, const std::string &s) {
  return String::NewFromUtf8(i, s.c_str()).ToLocalChecked();
}
static std::string utf(Isolate *i, Local<Value> v) {
  if (v.IsEmpty())
    return "execution_timeout";
  String::Utf8Value s(i, v);
  return *s ? *s : "";
}
static std::string json(Isolate *i, Local<Context> c, Local<Value> v) {
  Local<String> s;
  if (!JSON::Stringify(c, v).ToLocal(&s))
    return "{\"status\":\"error\",\"error\":\"result_not_serializable\"}";
  return utf(i, s);
}
static char *output(std::string s) {
  char *p = (char *)malloc(s.size() + 1);
  memcpy(p, s.c_str(), s.size() + 1);
  return p;
}
// The protocol argument list must stay an array even if scripts replace
// Object.prototype.toJSON. Individual values retain normal JSON semantics.
static MaybeLocal<Value> arguments(Isolate *i, Local<Context> c,
                                   Local<Value> value) {
  if (!value->IsArray()) {
    i->ThrowException(
        Exception::TypeError(str(i, "Host arguments must be an array")));
    return {};
  }
  auto input = value.As<Array>();
  auto copy = Array::New(i, input->Length());
  if (!copy->SetPrototype(c, Null(i)).FromMaybe(false))
    return {};
  for (uint32_t n = 0; n < input->Length(); n++) {
    Local<Value> entry;
    if (!input->Get(c, n).ToLocal(&entry))
      return {};
    if (!copy->CreateDataProperty(c, n, entry).FromMaybe(false))
      return {};
  }
  return copy;
}
static void host(const FunctionCallbackInfo<Value> &args) {
  auto *i = args.GetIsolate();
  auto c = i->GetCurrentContext();
  auto *r =
      (Runtime *)Local<External>::Cast(args.Data())->Value(kRuntimePointerTag);
  Local<Promise::Resolver> resolver;
  if (!Promise::Resolver::New(c).ToLocal(&resolver))
    return;
  int id = ++r->next;
  r->pending.emplace(id, Global<Promise::Resolver>(i, resolver));
  Local<Value> argument_list;
  if (!arguments(i, c, args[1]).ToLocal(&argument_list))
    return;
  auto o = Object::New(i);
  o->SetPrototype(c, Null(i)).FromMaybe(false);
  o->CreateDataProperty(c, str(i, "id"), Integer::New(i, id)).FromMaybe(false);
  o->CreateDataProperty(c, str(i, "method"), args[0]).FromMaybe(false);
  o->CreateDataProperty(c, str(i, "args"), argument_list).FromMaybe(false);
  Local<String> payload;
  if (!JSON::Stringify(c, o).ToLocal(&payload)) {
    r->pending.erase(id);
    return;
  }
  r->requests.push_back(utf(i, payload));
  args.GetReturnValue().Set(resolver->GetPromise());
}
static void host_sync(const FunctionCallbackInfo<Value> &args) {
  auto *i = args.GetIsolate();
  auto c = i->GetCurrentContext();
  auto *r =
      (Runtime *)Local<External>::Cast(args.Data())->Value(kRuntimePointerTag);
  Local<Value> argument_list;
  if (!arguments(i, c, args[1]).ToLocal(&argument_list))
    return;
  int id = ++r->next;
  auto o = Object::New(i);
  o->SetPrototype(c, Null(i)).FromMaybe(false);
  if (!o->CreateDataProperty(c, str(i, "id"), Integer::New(i, id))
           .FromMaybe(false) ||
      !o->CreateDataProperty(c, str(i, "method"), args[0]).FromMaybe(false) ||
      !o->CreateDataProperty(c, str(i, "args"), argument_list).FromMaybe(false))
    return;
  Local<String> payload;
  if (!JSON::Stringify(c, o).ToLocal(&payload))
    return;
  std::unique_lock<std::mutex> lock(r->mutex);
  r->sync_requests.push_back(utf(i, payload));
  r->cv.wait(lock,
             [r, id] { return r->expired || r->sync_responses.count(id); });
  if (r->expired)
    return;
  auto response = r->sync_responses.at(id);
  r->sync_responses.erase(id);
  lock.unlock();
  Local<Value> v;
  if (!JSON::Parse(c, str(i, response.first)).ToLocal(&v))
    return;
  if (response.second)
    i->ThrowException(v);
  else
    args.GetReturnValue().Set(v);
}
extern "C" {
__attribute__((visibility("default"))) char *sv8_sync_poll(Runtime *r) {
  std::lock_guard<std::mutex> lock(r->mutex);
  std::string q = "[";
  for (size_t n = 0; n < r->sync_requests.size(); n++) {
    if (n)
      q += ",";
    q += r->sync_requests[n];
  }
  r->sync_requests.clear();
  return output(q + "]");
}
__attribute__((visibility("default"))) void
sv8_sync_reply(Runtime *r, int id, const char *value, int rejected) {
  {
    std::lock_guard<std::mutex> lock(r->mutex);
    r->sync_responses[id] = {value, rejected != 0};
  }
  r->cv.notify_all();
}

__attribute__((visibility("default"))) void sv8_free(char *p) { free(p); }
__attribute__((visibility("default"))) const char *sv8_version() {
  return V8::GetVersion();
}
__attribute__((visibility("default"))) Runtime *sv8_create(int timeout_ms,
                                                           int heap_mb) {
  std::call_once(initialized, [] {
    runtime_platform = v8::platform::NewDefaultPlatform();
    V8::InitializePlatform(runtime_platform.get());
    V8::Initialize();
  });
  auto *r = new Runtime();
  r->allocator = ArrayBuffer::Allocator::NewDefaultAllocator();
  Isolate::CreateParams p;
  p.array_buffer_allocator = r->allocator;
  p.constraints.set_max_old_generation_size_in_bytes((size_t)heap_mb * 1024 *
                                                     1024);
  r->isolate = Isolate::New(p);
  r->isolate->SetMicrotasksPolicy(MicrotasksPolicy::kExplicit);
  {
    Locker locker(r->isolate);
    Isolate::Scope scope(r->isolate);
    HandleScope handles(r->isolate);
    auto c = Context::New(r->isolate);
    r->context.Reset(r->isolate, c);
    Context::Scope cs(c);
    c->Global()
        ->Set(c, str(r->isolate, "__sourceHostSync"),
              Function::New(c, host_sync,
                            External::New(r->isolate, r, kRuntimePointerTag))
                  .ToLocalChecked())
        .FromMaybe(false);
    c->Global()
        ->Set(c, str(r->isolate, "__sourceHost"),
              Function::New(c, host,
                            External::New(r->isolate, r, kRuntimePointerTag))
                  .ToLocalChecked())
        .FromMaybe(false);
  }
  r->timer = std::thread([r, timeout_ms] {
    std::unique_lock<std::mutex> l(r->mutex);
    if (!r->cv.wait_for(l, std::chrono::milliseconds(timeout_ms),
                        [r] { return r->done; })) {
      r->expired = true;
      r->isolate->TerminateExecution();
      r->cv.notify_all();
    }
  });
  return r;
}
__attribute__((visibility("default"))) void sv8_start(Runtime *r,
                                                      const char *script,
                                                      const char *variables,
                                                      const char *prelude) {
  // Cancellation can arrive before the worker enters V8. Do not consume its
  // termination exception while setting up a context or trying syntax
  // fallbacks.
  if (r->expired)
    return;
  auto *i = r->isolate;
  Locker locker(i);
  Isolate::Scope is(i);
  HandleScope hs(i);
  auto c = r->context.Get(i);
  Context::Scope cs(c);
  TryCatch tc(i);
  auto stopped = [&] { return r->expired || tc.HasTerminated(); };
  if (stopped())
    return;
  Local<Value> vars;
  if (!JSON::Parse(c, str(i, variables)).ToLocal(&vars)) {
    r->error = stopped() ? "execution_timeout" : utf(i, tc.Exception());
    return;
  }
  if (vars->IsObject()) {
    auto o = vars.As<Object>();
    Local<Array> keys;
    if (!o->GetOwnPropertyNames(c).ToLocal(&keys)) {
      r->error = stopped() ? "execution_timeout" : utf(i, tc.Exception());
      return;
    }
    for (uint32_t n = 0; n < keys->Length(); n++) {
      Local<Value> key, value;
      if (stopped() || !keys->Get(c, n).ToLocal(&key) ||
          !o->Get(c, key).ToLocal(&value) ||
          !c->Global()->Set(c, key, value).FromMaybe(false)) {
        r->error = stopped() ? "execution_timeout" : utf(i, tc.Exception());
        return;
      }
    }
  }
  if (stopped())
    return;
  std::string bootstrap =
      "globalThis.source = new Proxy(function(){}, {get:(_, "
      "k)=>k==='call'?__sourceHost:__sourceProxy(String(k))}); function "
      "__sourceProxy(path){return new "
      "Proxy(function(){},{get:(_,k)=>__sourceProxy(path+'.'+String(k)),apply:("
      "_,t,args)=>__sourceHost(path,args)});}";
  Local<Script> b;
  Local<Value> unused;
  if (!Script::Compile(c, str(i, bootstrap + prelude)).ToLocal(&b) ||
      stopped() || !b->Run(c).ToLocal(&unused)) {
    r->error = stopped() ? "execution_timeout" : utf(i, tc.Exception());
    return;
  }
  if (stopped())
    return;
  std::string code =
      "(async()=>{ return await (" + std::string(script) + "\n); })()";
  Local<Script> s;
  Local<Value> result;
  if (!Script::Compile(c, str(i, code)).ToLocal(&s)) {
    // Only ordinary syntax errors may trigger the statement-body fallback.
    // Resetting a caught termination would allow cancelled code to run again.
    if (stopped() || !tc.CanContinue()) {
      r->error = "execution_timeout";
      return;
    }
    tc.Reset();
    code = "(async()=>{ " + std::string(script) + "\n })()";
    if (!Script::Compile(c, str(i, code)).ToLocal(&s)) {
      r->error = utf(i, tc.Exception());
      return;
    }
  }
  if (stopped() || !s->Run(c).ToLocal(&result)) {
    r->error = r->expired ? "execution_timeout" : utf(i, tc.Exception());
    return;
  }
  r->result.Reset(i, result.As<Promise>());
  i->PerformMicrotaskCheckpoint();
}
__attribute__((visibility("default"))) char *sv8_poll(Runtime *r) {
  if (r->expired)
    return output("{\"status\":\"error\",\"error\":\"execution_timeout\"}");
  auto *i = r->isolate;
  Locker locker(i);
  Isolate::Scope is(i);
  HandleScope hs(i);
  auto c = r->context.Get(i);
  Context::Scope cs(c);
  TryCatch tc(i);
  i->PerformMicrotaskCheckpoint();
  if (!r->error.empty()) {
    auto o = Object::New(i);
    o->SetPrototype(c, Null(i)).FromMaybe(false);
    o->CreateDataProperty(c, str(i, "status"), str(i, "error"))
        .FromMaybe(false);
    o->CreateDataProperty(c, str(i, "error"), str(i, r->error))
        .FromMaybe(false);
    return output(json(i, c, o));
  }
  if (r->expired)
    return output("{\"status\":\"error\",\"error\":\"execution_timeout\"}");
  if (r->result.IsEmpty())
    return output("{\"status\":\"error\",\"error\":\"runtime_start_failed\"}");
  auto p = r->result.Get(i);
  if (p->State() != Promise::kPending) {
    auto o = Object::New(i);
    o->SetPrototype(c, Null(i)).FromMaybe(false);
    bool ok = p->State() == Promise::kFulfilled;
    o->CreateDataProperty(c, str(i, "status"), str(i, ok ? "done" : "error"))
        .FromMaybe(false);
    o->CreateDataProperty(c, str(i, ok ? "value" : "error"),
                          ok ? p->Result() : str(i, utf(i, p->Result())))
        .FromMaybe(false);
    return output(json(i, c, o));
  }
  std::string q = "{\"status\":\"pending\",\"requests\":[";
  for (size_t n = 0; n < r->requests.size(); n++) {
    if (n)
      q += ",";
    q += r->requests[n];
  }
  r->requests.clear();
  return output(q + "]}");
}
__attribute__((visibility("default"))) void
sv8_resolve(Runtime *r, int id, const char *value, int rejected) {
  if (r->expired)
    return;
  auto *i = r->isolate;
  Locker locker(i);
  Isolate::Scope is(i);
  HandleScope hs(i);
  auto c = r->context.Get(i);
  Context::Scope cs(c);
  auto it = r->pending.find(id);
  if (it == r->pending.end())
    return;
  Local<Value> v;
  if (!JSON::Parse(c, str(i, value)).ToLocal(&v))
    v = Null(i);
  auto resolver = it->second.Get(i);
  if (rejected)
    resolver->Reject(c, v).FromMaybe(false);
  else
    resolver->Resolve(c, v).FromMaybe(false);
  it->second.Reset();
  r->pending.erase(it);
  i->PerformMicrotaskCheckpoint();
}
__attribute__((visibility("default"))) void sv8_cancel(Runtime *r) {
  r->expired = true;
  r->isolate->TerminateExecution();
  r->cv.notify_all();
}
__attribute__((visibility("default"))) void sv8_destroy(Runtime *r) {
  {
    std::lock_guard<std::mutex> l(r->mutex);
    r->done = true;
  }
  r->cv.notify_one();
  r->timer.join();
  {
    Locker locker(r->isolate);
    r->isolate->CancelTerminateExecution();
    for (auto &p : r->pending)
      p.second.Reset();
    r->result.Reset();
    r->context.Reset();
  }
  r->isolate->Dispose();
  delete r->allocator;
  delete r;
}
}
