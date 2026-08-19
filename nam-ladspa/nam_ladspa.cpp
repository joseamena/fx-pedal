// LADSPA wrapper around NeuralAmpModelerCore (github.com/sdatkinson/NeuralAmpModelerCore).
//
// Model selection: LADSPA has no string/file ports, so the .nam model path is resolved
// once at instantiate() time from the NAM_MODEL_PATH environment variable, falling back
// to ~/.config/fx-pedal/nam_model.nam. Point either at a new file and restart the pedal
// (PipeWire filter-chain process) to switch models -- no rebuild needed. fx-pedal itself
// manages that fallback path as a symlink via its `nam-model get/set` command; see
// fx_core.py's nam_model_set() if you're integrating this with something other than
// fx-pedal.
//
// Mono in / mono out (fx-pedal's LADSPA host only accepts single audio-in/audio-out
// plugins), plus two control ports (input/output trim in dB) for level matching between
// captures of different models.

#include <ladspa.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <memory>
#include <vector>

#include "NAM/dsp.h"
#include "NAM/get_dsp.h"

namespace
{

constexpr unsigned long kPortInput = 0;
constexpr unsigned long kPortOutput = 1;
constexpr unsigned long kPortInputGainDb = 2;
constexpr unsigned long kPortOutputGainDb = 3;
constexpr unsigned long kPortCount = 4;

// Matches NAM_DEFAULT_MAX_BUFFER_SIZE; also the chunk size used to process host
// blocks larger than this in a loop, so any host block size is handled safely.
constexpr int kMaxBlockSize = 4096;

std::string ResolveModelPath()
{
  if (const char* env = std::getenv("NAM_MODEL_PATH"); env != nullptr && env[0] != '\0')
    return env;
  const char* home = std::getenv("HOME");
  return std::string(home != nullptr ? home : "/home/arduino") + "/.config/fx-pedal/nam_model.nam";
}

struct NamLadspaInstance
{
  std::unique_ptr<nam::DSP> model;
  double sampleRate = 48000.0;
  bool bypass = false;

  LADSPA_Data* portInput = nullptr;
  LADSPA_Data* portOutput = nullptr;
  LADSPA_Data* portInputGainDb = nullptr;
  LADSPA_Data* portOutputGainDb = nullptr;

  std::vector<double> inChunk = std::vector<double>(kMaxBlockSize, 0.0);
  std::vector<double> outChunk = std::vector<double>(kMaxBlockSize, 0.0);
};

LADSPA_Handle Instantiate(const LADSPA_Descriptor*, unsigned long sampleRate)
{
  auto* inst = new NamLadspaInstance();
  inst->sampleRate = static_cast<double>(sampleRate);

  const std::string modelPath = ResolveModelPath();
  try
  {
    inst->model = nam::get_dsp(std::filesystem::path(modelPath));
  }
  catch (const std::exception& e)
  {
    std::fprintf(stderr, "nam_ladspa: failed to load model '%s': %s\n", modelPath.c_str(), e.what());
  }

  if (inst->model == nullptr)
  {
    std::fprintf(stderr, "nam_ladspa: no model loaded (NAM_MODEL_PATH='%s'); running in bypass mode\n",
                 modelPath.c_str());
    inst->bypass = true;
  }
  else
  {
    nam::activations::Activation::enable_fast_tanh();
    inst->model->Reset(inst->sampleRate, kMaxBlockSize);
  }

  return inst;
}

void ConnectPort(LADSPA_Handle handle, unsigned long port, LADSPA_Data* dataLocation)
{
  auto* inst = static_cast<NamLadspaInstance*>(handle);
  switch (port)
  {
    case kPortInput: inst->portInput = dataLocation; break;
    case kPortOutput: inst->portOutput = dataLocation; break;
    case kPortInputGainDb: inst->portInputGainDb = dataLocation; break;
    case kPortOutputGainDb: inst->portOutputGainDb = dataLocation; break;
    default: break;
  }
}

void Activate(LADSPA_Handle handle)
{
  auto* inst = static_cast<NamLadspaInstance*>(handle);
  if (inst->model)
    inst->model->Reset(inst->sampleRate, kMaxBlockSize);
}

void Run(LADSPA_Handle handle, unsigned long sampleCount)
{
  auto* inst = static_cast<NamLadspaInstance*>(handle);

  const float inGainDb = inst->portInputGainDb != nullptr ? *inst->portInputGainDb : 0.0f;
  const float outGainDb = inst->portOutputGainDb != nullptr ? *inst->portOutputGainDb : 0.0f;
  const double inGain = std::pow(10.0, inGainDb / 20.0);
  const double outGain = std::pow(10.0, outGainDb / 20.0);

  if (inst->bypass || !inst->model)
  {
    const auto gain = static_cast<LADSPA_Data>(inGain * outGain);
    for (unsigned long i = 0; i < sampleCount; i++)
      inst->portOutput[i] = inst->portInput[i] * gain;
    return;
  }

  unsigned long processed = 0;
  while (processed < sampleCount)
  {
    const int chunk = static_cast<int>(std::min<unsigned long>(kMaxBlockSize, sampleCount - processed));

    for (int i = 0; i < chunk; i++)
      inst->inChunk[i] = static_cast<double>(inst->portInput[processed + i]) * inGain;

    double* inPtr = inst->inChunk.data();
    double* outPtr = inst->outChunk.data();
    inst->model->process(&inPtr, &outPtr, chunk);

    for (int i = 0; i < chunk; i++)
      inst->portOutput[processed + i] = static_cast<LADSPA_Data>(inst->outChunk[i] * outGain);

    processed += static_cast<unsigned long>(chunk);
  }
}

void Cleanup(LADSPA_Handle handle)
{
  delete static_cast<NamLadspaInstance*>(handle);
}

LADSPA_PortDescriptor gPortDescriptors[kPortCount] = {
  LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
  LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
  LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
  LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
};

const char* gPortNames[kPortCount] = {
  "Input",
  "Output",
  "Input Gain (dB)",
  "Output Gain (dB)",
};

LADSPA_PortRangeHint gPortRangeHints[kPortCount] = {
  {0, 0.0f, 0.0f},
  {0, 0.0f, 0.0f},
  {LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE | LADSPA_HINT_DEFAULT_0, -20.0f, 20.0f},
  {LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE | LADSPA_HINT_DEFAULT_0, -20.0f, 20.0f},
};

// Arbitrary locally-unique ID (not registered with any central LADSPA ID authority;
// fx-pedal identifies plugins by .so path + label, not by this ID, so that's fine here).
const LADSPA_Descriptor gDescriptor = {
  /* UniqueID */ 5000001,
  /* Label */ "nam_amp",
  /* Properties */ 0,
  /* Name */ "Neural Amp Modeler (NAM Core)",
  /* Maker */ "NeuralAmpModelerCore by sdatkinson; LADSPA wrapper by Jose A. Mena",
  /* Copyright */ "Wrapper: MIT. Model weights: per the .nam file's own license.",
  /* PortCount */ kPortCount,
  /* PortDescriptors */ gPortDescriptors,
  /* PortNames */ gPortNames,
  /* PortRangeHints */ gPortRangeHints,
  /* ImplementationData */ nullptr,
  /* instantiate */ Instantiate,
  /* connect_port */ ConnectPort,
  /* activate */ Activate,
  /* run */ Run,
  /* run_adding */ nullptr,
  /* set_run_adding_gain */ nullptr,
  /* deactivate */ nullptr,
  /* cleanup */ Cleanup,
};

} // namespace

extern "C" __attribute__((visibility("default"))) const LADSPA_Descriptor* ladspa_descriptor(unsigned long index)
{
  return index == 0 ? &gDescriptor : nullptr;
}
