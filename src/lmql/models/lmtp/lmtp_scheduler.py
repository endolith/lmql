import asyncio
import json
import pickle
import sys
import threading
import time
from dataclasses import dataclass
from queue import Empty as QueueEmpty
from queue import Queue
from weakref import WeakValueDictionary

import numpy as np

import lmql.models.lmtp.backends as backends
import lmql.utils.nputil as nputil
from lmql.models.lmtp.backends.lmtp_model import LMTPModel

class LMTPCannotLoadModelByPolicy(Exception):
    pass

@dataclass
class GenerateCall:
    prompt: str
    logit_bias: dict
    kwargs: dict
    stream_id: int
    result_queue: Queue

    cancelled: bool = False

    def put(self, token):
        self.result_queue.put(("TOKEN", token))

    def cancel(self):
        self.cancelled = True

    def error(self, msg):
        self.result_queue.put(("TOKEN", {
            "stream_id": self.stream_id,
            "error": msg
        }))

    def generation_mode(self):
        is_score = self.kwargs.get("score", False)
        if is_score: return "score"
        
        key_args = self.kwargs.copy()
        key_args.pop("max_tokens", None)
        key_args.pop("top_logprobs", None)
        key_args.setdefault("temperature", 0.0)
        
        # string that describes the generation mode for this call (same string = can run in batch)
        return "generate-" + "-".join("{}-{}".format(k, v) for k, v in sorted(key_args.items()))

@dataclass
class GenerateBatch:
    input_ids: list
    attention_mask: list
    
    temperature: float
    max_tokens: int
    logit_biases: list

    calls: list

    is_score: bool = False
    scoring_offsets: list = None
    kwargs: dict = None
    
    @classmethod
    def from_calls(cls, calls):
        input_ids = [c.prompt for c in calls]
        max_len = max(len(ids) for ids in input_ids)
        
        attention_mask = [[0] * (max_len - len(ids)) + [1] * len(ids) for ids in input_ids]
        input_ids = [[0] * (max_len - len(ids)) + ids for ids in input_ids]
        
        temperature = calls[0].kwargs.get("temperature", 0.0)
        max_tokens = max(c.kwargs.get("max_tokens", 32) for c in calls)
        logit_biases = [c.logit_bias or {} for c in calls]
        
        is_score = any(c.kwargs.get("score", False) for c in calls)
        assert not is_score or all(c.kwargs.get("score", False) for c in calls), "cannot mix score and non-score calls in batch"
        
        if is_score:
            scoring_offsets = []
            for i, c in enumerate(calls):
                padding = max_len - len(c.prompt)
                scoring_offsets.append(c.kwargs.get("scoring_offset", 0) + padding)
        else:
            scoring_offsets = None

        # other kwargs (e.g. top_k, repetition_penalty, etc.)
        kwargs = calls[0].kwargs.copy()
        kwargs.pop("max_tokens", None)
        kwargs.pop("top_logprobs", None)
        kwargs.pop("temperature", None)

        return cls(input_ids, attention_mask, temperature, max_tokens, logit_biases, calls, is_score, scoring_offsets, kwargs)

    def cancelled(self):
        return not any(not c.cancelled for c in self.calls)

    def generate_args(self):
        return {
            "input_ids": self.input_ids,
            "attention_mask": self.attention_mask,
            "temperature": self.temperature,
            "max_new_tokens": self.max_tokens,
            "bias_tensor": self.logit_biases if len(self.logit_biases) > 0 else None,
            **self.kwargs
        }

class ScoreStreamer:
    def log_token(self, batch: GenerateBatch, all_scores, **kwargs):
        batch_size = all_scores.shape[0]
        
        for i in range(batch_size):
            offset = batch.scoring_offsets[i]
            scores = all_scores[i][offset:]
            scored_ids = batch.input_ids[i][offset:]
            call = batch.calls[i]

            for j, (score, token) in enumerate(zip(scores, scored_ids)):
                token_payload = {
                    "token": int(token),
                    "stream_id": call.stream_id,
                    "logprob": float(score),
                    "finish_reason": "stop" if j == len(scores) - 1 else None
                }
                call.put(token_payload)

class TokenStreamer:
    def __init__(self, batch: GenerateBatch, eos_token_id, scheduler, cancels=True):
        self.batch = batch
        self.cancels = cancels
        self.eos_token_id = eos_token_id
        self.scheduler = scheduler

    def __call__(self, input_ids, scores, **kwargs) -> bool:
        self.log_token(input_ids, scores, **kwargs)
        return False

    def log_token(self, input_ids, scores, last=False, **kwargs):
        batch_size = input_ids.shape[0]
        
        last_tokens = input_ids[:, -1]
        last_scores = scores[-1]

        max_num_top_logprobs = max([c.kwargs.get("top_logprobs", 1) for c in self.batch.calls])

        if not nputil.is_array(last_tokens):
            last_tokens = last_tokens.cpu().numpy()
        if not nputil.is_array(last_scores):
            last_scores = last_scores.cpu().numpy()

        # check for cancelled calls
        if self.batch.cancelled() and self.cancels:
            raise InterruptedError("inference calls cancelled")

        self.scheduler.measure_token(batch_size)

        # for each sample get top logprobs
        all_logprobs, all_indices  = nputil.topk(last_scores, max_num_top_logprobs, sorted=True, axis=-1)

        for i in range(batch_size):
            logprobs = all_logprobs[i]
            tokens = all_indices[i]
            token_score = last_scores[i][last_tokens[i]]

            top_logprobs = {
                int(last_tokens[i]): float(token_score.item()),
                **{int(token.item()): float(logprob.item()) for logprob, token in zip(logprobs, tokens)}
            }

            num_top_logprobs = self.batch.calls[i].kwargs.get("top_logprobs", 1)
            logprobs = logprobs[:num_top_logprobs]
            tokens = tokens[:num_top_logprobs]

            token_payload = {
                "token": int(last_tokens[i].item()),
                "stream_id": self.batch.calls[i].stream_id,
                "logprob": float(token_score.item()),
                "finish_reason": ("stop" if last_tokens[i].item() == self.eos_token_id else "length" if last else None),
                "top_logprobs": top_logprobs
            }
            
            self.batch.calls[i].put(token_payload)

class Scheduler:
    """
    A scheduler that takes calls to generate and batches them together. 

    Can be shared among multiple clients, make sure to call unregister(<user>) when done,
    to allow the scheduler to shut down when no longer needed.
    """
    def __init__(self, model_identifier, model_args = None, sync=False):
        self.model_identifier = model_identifier
        if model_args is None: self.model_args = {}
        else: self.model_args = model_args
        
        self.queue = Queue()
        self.kill_event = threading.Event()
        
        self.sync = sync

        if not self.sync:
            self.worker_thread = threading.Thread(target=self.worker, daemon=True, name="scheduler-worker")
            self.worker_thread.start()
        else:
            pass

        self.users = set()
        self.last_use = time.time()

        # set once initialized
        self._model_info = "<unavailable>"

        # size of active batch
        self.active_batch_size = 0

        self.last_token_times = []
        self.last_batch_sizes = []
        
        self.last_tok_s = 0.0
        self.last_batch_size = 0.0

    def measure_token(self, batch_size):
        self.last_token_times.append(time.time())
        self.last_batch_sizes.append(batch_size)

        if len(self.last_token_times) > 100:
            self.last_token_times.pop(0)
            self.last_batch_sizes.pop(0)

        samples_to_consider = [v for i,v in enumerate(self.last_batch_sizes) if self.last_token_times[i] > time.time() - 1.0]
        token_in_last_second = sum(samples_to_consider)
        self.last_tok_s = self.last_tok_s * 0.9 + token_in_last_second * 0.1
        
        avg_batch_size = np.mean(self.last_batch_sizes[-len(samples_to_consider):])
        self.last_batch_size = self.last_batch_size * 0.9 + avg_batch_size * 0.1

        print("[streaming at {:.2f} tok/s, average batch size {:.2f}]".format(self.last_tok_s, self.last_batch_size), flush=True, end="\r")

    def model_info(self):
        return self._model_info

    def put(self, call: GenerateCall):
        self.queue.put(call)

    def cancel_stream(self, stream_id):
        pass

    def batches(self, max_batch_size=8):
        start = time.time()
        calls = []
        # get as many calls from the queue as possible within 0.1 seconds
        first = True
        while time.time() - start < 0.1:
            try:
                calls.append(self.queue.get(block=first, timeout=0.1))
                first = False
            except QueueEmpty:
                break
        # group calls into batches
        batches_by_mode = {}
        for c in calls:
            mode = c.generation_mode()
            batches_by_mode.setdefault(mode, []).append(c)
        
        # split batches that are too large
        fitting_batches = []
        for mode, batches in batches_by_mode.items():
            if len(batches) > max_batch_size:
                for i in range(0, len(batches), max_batch_size):
                    fitting_batches.append(batches[i:i+max_batch_size])
            else:
                fitting_batches.append(batches)

        return fitting_batches

    async def async_worker(self):
        """
        Can be used when self.sync, in place of worker_thread.

        Runs the model asynchronously on the main thread. Blocks remaining application
        during model calls. As a consequence, tokens are only streamed when the model
        has finished generating the current batch.
        """

        model = LMTPModel.load(self.model_identifier, **self.model_args)
        self._model_info = model.model_info()
        
        idle_shown = False
        idle_start = time.time()

        while True:
            if self.kill_event.is_set():
                break

            if self.queue.empty():
                await asyncio.sleep(0.01)
                if not idle_shown:
                    idle_shown = True
                    idle_start = time.time()
                    print("\n[Idle]                                                               ", flush=True, end="\r")
                continue
            
            print("[Idle for {:.2f}s]                          ".format(time.time() - idle_start), flush=True, end="\n")
            idle_shown = False
            await self.process_batch(model)
    
    def worker(self):
        """
        Can be used when not self.sync, in place of async_worker.

        Runs the model on a separate thread. Does not block the application during model
        calls. As a consequence, tokens are streamed as soon as they are generated.

        This is the default mode and the thread is started automatically when instantiating
        the scheduler with sync=False.
        """
        asyncio.run(self.async_worker())

    async def process_batch(self, model):
        for batch in self.batches(model.max_batch_size):
            try:
                b = GenerateBatch.from_calls(batch)
                
                if b.is_score:
                    kwargs = b.generate_args()
                    input_ids = kwargs["input_ids"]
                    attention_mask = kwargs["attention_mask"]
                    
                    scores = model.score(input_ids, attention_mask)
                    ScoreStreamer().log_token(b, scores)
                else:
                    streamer = TokenStreamer(b, model.eos_token_id, scheduler=self, cancels=model.cancellable)
                    kwargs = b.generate_args()
                    
                    kwargs["input_ids"] = np.array(kwargs["input_ids"], dtype=np.int64)
                    kwargs["attention_mask"] = np.array(kwargs["attention_mask"], dtype=np.int32)

                    result = await model.generate(**kwargs, streamer=streamer)
                    streamer.log_token(result.sequences, result.scores, last=True)
            except InterruptedError:
                for c in batch:
                    c.error("lmtp.cancelled")
            except Exception as e:
                import traceback
                traceback.print_exc()
                print("[Error during generate()]", e, flush=True)
                for c in batch:
                    c.error("failed to generate tokens '" + str(e) + "'")

    @staticmethod
    def instance(model_identifier, model_args, user, only_existing=False, sync=False):
        identifier = (model_identifier, pickle.dumps(model_args).hex())

        if identifier not in Scheduler._instances:
            if only_existing:
                raise LMTPCannotLoadModelByPolicy("Model '" + model_identifier + "' is not loaded and server is not configured to load it on demand.")
            Scheduler._instances[identifier] = Scheduler(model_identifier, model_args, sync=sync)

        s = Scheduler._instances[identifier]
        s.last_use = time.time()
        
        if user is not None:
            s.users.add(user)

        Scheduler.gc() # unload any unused models
    
        return s
    
    def unregister(self, user):
        if user in self.users:
            self.users.remove(user)
            self.last_use = time.time()

    def dealloc(self):
        print("[Unloading ", self.model_identifier, "]", flush=True, sep="")
        identifier = (self.model_identifier, pickle.dumps(self.model_args).hex())
        
        Scheduler._instances.pop(identifier)
        
        self.kill_event.set()
        self.worker_thread.join()

    @staticmethod
    def gc(n: int = 2, timeout: int = 10):
        """
        Manually Scheduler garbage collection. Unloads models that have not been used
        but only if there are more than n models loaded. This keeps the model around
        even if it is not used for a while, but unloads it when another model is loaded.
        """

        total = len(Scheduler._instances)
        not_needed = [k for k, v in Scheduler._instances.items() if len(v.users) == 0]

        if total >= n:
            for k in not_needed:
                s = Scheduler._instances[k]
                Scheduler._instances[k].dealloc()

Scheduler._instances = {}

class TokenSession:
    """
    A LMTP token session, which is a single user generating tokens with a fixed model, 
    using several token streams in parallel and varying sampling configurations.
    """
    def __init__(self, transport, model_args, static=False, longrunning=False):
        self.transport = transport
        self.output_stream = Queue()
        self.queue_processor = asyncio.create_task(self.queue_loop())
        self.used_models = set()
        self.model_args = model_args
        self.static = static

        self.longrunning = longrunning
        
        self.active_stream = WeakValueDictionary()

    async def handle(self, cmd, kwargs):
        stream_id = kwargs.get("stream_id")
        model = kwargs.pop("model")

        try:
            if cmd == "GENERATE":
                prompt = kwargs.pop("prompt")
                stream_id = kwargs.pop("stream_id")
                logit_bias = kwargs.pop("logit_bias", {})
                self.used_models.add(model)

                scheduler = Scheduler.instance(model, self.model_args, user=self, only_existing=self.static)
                call = GenerateCall(prompt, logit_bias, kwargs, stream_id, self.output_stream)
                self.active_stream[stream_id] = call
                scheduler.put(call)
            elif cmd == "SCORE":
                prompt = kwargs.pop("prompt")
                scored = kwargs.pop("scored")
                stream_id = kwargs.pop("stream_id")
                self.used_models.add(model)
                
                kwargs["score"] = True
                # full sequence to score
                full_ids = prompt + scored 
                # determines the offset from which on the scoring starts in full_ids
                kwargs["scoring_offset"] = len(prompt)

                scheduler = Scheduler.instance(model, self.model_args, user=self, only_existing=self.static)
                call = GenerateCall(full_ids, {}, kwargs, stream_id, self.output_stream)
                self.active_stream[stream_id] = call
                scheduler.put(call)
            elif cmd == "MODEL_INFO":
                scheduler = Scheduler.instance(model, self.model_args, user=self, only_existing=self.static)
                self.output_stream.put(("MSG", {
                    "stream_id": stream_id,
                    "model_info": scheduler.model_info()
                }))
            elif cmd == "CANCEL":
                msg_id = kwargs.pop("stream_id")
                
                stream_id = kwargs.get("data", {}).get("stream_id")
                # print("cancel", stream_id, flush=True)
                if stream_id in self.active_stream.keys():
                    call = self.active_stream.pop(stream_id)
                    if call is not None:
                        call.cancel()
                    
                    self.output_stream.put(("MSG", {
                        "stream_id": msg_id,
                        "message": "cancel requested"
                    }))
                else:
                        self.output_stream.put(("MSG", {
                        "stream_id": msg_id,
                        "message": "no active stream with id " + str(stream_id)
                    }))
            else:
                raise Exception("Unknown command: {}".format(cmd))
        except LMTPCannotLoadModelByPolicy as e:
            print("Client requested model that is not loaded and server is not configured to load it on demand.", flush=True)
            self.output_stream.put(("MSG", {
                "stream_id": stream_id,
                "error": "The requested model is not loaded and the server is not configured to load it on demand."
            }))
        except Exception as e:
            print("Error in lmtp_server.TokenSession.handle", e, flush=True)
            self.output_stream.put(("MSG", {
                "stream_id": stream_id,
                "error": str(e)
            }))

    async def queue_loop(self):
        try:
            while True:
                try:
                    msg_type, token = self.output_stream.get_nowait()
                    await self.transport.send(msg_type, token)
                except QueueEmpty:
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.close()
        except Exception as e:
            print("TokenSession.queue_loop error", e, flush=True)
            self.close()

    def close(self):
        self.queue_processor.cancel()
        
        # cancel all active streams
        for call in self.active_stream.values():
            call.cancel()
        
        for m in self.used_models:
            try:
                scheduler = Scheduler.instance(m, model_args=self.model_args, user=None, only_existing=self.static)
                scheduler.unregister(self)
                
                if self.longrunning:
                    scheduler.gc()
                else:
                    Scheduler.gc(0)
            except LMTPCannotLoadModelByPolicy:
                pass