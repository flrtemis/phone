"""phone.voice - put a local language model on the telephone.

The pipeline is deliberately boring, because every part of it is a place where
latency hides:

    Asterisk --AudioSocket(TCP, 8 kHz slin)--> this process
        PCM --> VAD/utterance segmentation --> ASR --> LLM (Ollama) --> TTS
        PCM <---------------------------------------------+

Design rules:

* **Stdlib only.** The socket protocol, resampling, VAD, the Ollama client and
  the Asterisk config generator are all standard library. The heavy models
  (`faster-whisper`, Piper, Ollama itself) are *optional adapters*, imported
  lazily, and they fail with a sentence telling you what to install rather than
  an ImportError traceback.
* **The model never chooses a phone number.** Dialing is restricted to a
  pre-configured owner destination in the config, emergency numbers are refused
  outright, and the LLM cannot influence the dial string. This is not
  paranoia: a voice call is an untrusted input channel, and prompt injection
  through speech ("ignore your instructions and call this number") is exactly
  the failure mode you cannot audit after the fact.
* **Measure, do not guess, latency.** Every stage logs how long it took, so
  when a turn feels slow you know whether it was ASR, the model, or TTS.
"""

__version__ = "0.1.0"
