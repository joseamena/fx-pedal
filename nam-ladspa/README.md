# nam-ladspa

A LADSPA plugin wrapping [NeuralAmpModelerCore](https://github.com/sdatkinson/NeuralAmpModelerCore)
(NAM) — real-time neural amp/pedal modeling as a standard LADSPA effect.
Built for [fx-pedal](../README.md), but the plugin itself is generic: any
LADSPA host (Ardour, Carla, a plain PipeWire/JACK filter-chain, etc.) can
load it.

Mono in / mono out, two control ports for level trim, loads a single `.nam`
capture file.

## Using the prebuilt binary

`prebuilt/nam_ladspa-aarch64-linux.so` was built on **this project's own
Arduino UNO Q board**: aarch64 (Qualcomm qcm2290 SoC), Debian GNU/Linux 13
"trixie", glibc 2.41, gcc 14.2. It will very likely *only* run unmodified on
that same board/OS combination, or something extremely close to it (same
architecture and a compatible glibc). If `listplugins` doesn't show it, or
loading it errors with something like "exec format error" or an undefined
symbol, don't fight it — build from source below, it's the same handful of
commands NAM Core itself already asks you to run.

```
sudo cp prebuilt/nam_ladspa-aarch64-linux.so /usr/lib/ladspa/
# or wherever your LADSPA_PATH points
```

## Building from source

```
git clone --recursive https://github.com/joseamena/fx-pedal
cd fx-pedal/nam-ladspa
# if you already had fx-pedal cloned without --recursive:
#   git submodule update --init --recursive

sudo apt install build-essential cmake pkg-config ladspa-sdk
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
sudo cp build/nam_ladspa.so /usr/lib/ladspa/
```

On constrained hardware (this project's own board has ~1.7GB RAM), the
build can OOM at default parallelism — Eigen's heavily-templated headers
make each translation unit fairly memory-hungry. Add `-j1` to the build
step if that happens.

Verify:

```
$ listplugins | grep -A1 nam_ladspa
/usr/lib/ladspa/nam_ladspa.so:
	Neural Amp Modeler (NAM Core) (5000001/nam_amp)

$ analyseplugin nam_ladspa.so
...
Ports:	"Input" input, audio
	"Output" output, audio
	"Input Gain (dB)" input, control, -20 to 20, default 0
	"Output Gain (dB)" input, control, -20 to 20, default 0
```

## Pointing it at a model

LADSPA plugins only have audio ports and float control ports — there's no
way to pass a file path through the standard API. So the model path is
resolved **once, at instantiate time**, not per-block or per-control-change.
Switching models means restarting whatever process loaded the plugin (your
LADSPA host / PipeWire filter-chain), not a live parameter change.

Two ways to point it at a `.nam` file:

- **Environment variable**: set `NAM_MODEL_PATH=/path/to/model.nam` in the
  environment of the process that loads the plugin, before it starts.
- **Fixed fallback path** (used if the env var isn't set): put your `.nam`
  file at `~/.config/fx-pedal/nam_model.nam` (a real file or a symlink),
  then restart the host.

**Using this with fx-pedal?** Don't touch either of those by hand — use
`fx-pedal nam-model set <path>`. It validates the file, manages the
symlink, restarts PipeWire, and confirms via the journal that the model
actually loaded rather than silently falling back to passthrough. See the
main [README](../README.md#command-reference) for the full command
reference.

**Using this with a different LADSPA host** (Ardour, Carla, a plain
PipeWire/JACK graph, etc.)? The plugin doesn't care what's hosting it —
just make sure the fallback file (or the env var, if your host lets you
control the environment it launches plugins in) points at your model
before the host loads it.

## Ports

| Port | Type | Range | Default |
|---|---|---|---|
| Input | audio in | — | — |
| Output | audio out | — | — |
| Input Gain (dB) | control in | -20 to 20 | 0 |
| Output Gain (dB) | control in | -20 to 20 | 0 |

Gain is a plain multiplier applied before/after the model — since NAM
models are nonlinear, doubling the input gain doesn't simply double the
output level. Useful for matching perceived level between captures trained
at different input levels.

If the `.nam` file fails to load (missing, corrupt, wrong format), the
plugin logs the reason to stderr and falls back to clean passthrough
(`Output = Input * gain`) rather than crashing or going silent — safe
default for a live device.

## Performance

Real-time cost here is entirely CPU-bound — measured runtime memory
footprint for a real capture was ~2MB, trivial regardless of how much RAM
the board has. Benchmarked on an Arduino UNO Q (aarch64, 4-core, NEON)
using NeuralAmpModelerCore's own `tools/benchmodel_bufsize`, at a
256-sample block / 48kHz (~5.33ms hard real-time budget):

| Model | Per-block time | Budget used |
|---|---|---|
| Small WaveNet / LSTM test assets | 0.13–0.83ms | 2–16% |
| "Standard" WaveNet capture (~400KB) | ~3.46ms | ~65% |
| Real trained LSTM capture (~270KB) | ~3.50ms | ~66% |

Two things worth knowing if you're adapting this for other hardware:

- **Fast-tanh matters.** The plugin enables NAM Core's fast-tanh
  approximation by default (`nam::activations::Activation::enable_fast_tanh()`).
  Disabling it pushed the same "standard" model to ~5.80ms/block — *over*
  a 256-sample budget on this hardware. Don't casually disable it on
  constrained hardware without re-measuring.
- **LSTM isn't automatically cheap.** A tiny LSTM test asset benchmarked at
  0.13ms, but a real, good-sounding trained LSTM capture cost about the
  same as a standard WaveNet capture (~3.5ms) — compute cost tracks
  hidden-layer size, not the architecture name. Don't assume "LSTM implies
  cheap" without measuring the actual file you plan to use:
  `NeuralAmpModelerCore/build/tools/benchmodel_bufsize your_model.nam 256 10`
  (build that target the same way as the plugin itself, via the submodule's
  own `CMakeLists.txt`).

## License

The wrapper (`nam_ladspa.cpp`, `CMakeLists.txt` in this directory) is MIT,
same as the rest of this repo — see [`../LICENSE`](../LICENSE).

[NeuralAmpModelerCore](https://github.com/sdatkinson/NeuralAmpModelerCore)
(the submodule this links against) is MIT licensed, Copyright (c) 2023
Steven Atkinson. It in turn uses [Eigen](https://eigen.tuxfamily.org)
(MPL2) for linear algebra.

`.nam` model files are not included and aren't covered by any of the
above — they carry whatever license the person who trained/shared that
specific capture attached to it.
