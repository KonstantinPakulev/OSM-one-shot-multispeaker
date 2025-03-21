from scipy.ndimage.morphology import binary_dilation
from pathlib import Path
from typing import Optional, Union
from warnings import warn
import numpy as np
import librosa
import struct
import yaml

try:
    import webrtcvad
except:
    warn("Unable to import 'webrtcvad'. This package enables noise removal and is recommended.")
    webrtcvad=None


class WavPreprocessor(object):
    """Interface """
    def __init__(self, audio_config_yaml_path):
        with open(audio_config_yaml_path, "r") as ymlfile:
            self.audio_config_yaml = yaml.load(ymlfile)

    def __call__(self, *args, **kwargs):

        return self.preprocess_wav(*args, **kwargs)

    def preprocess_wav(self, *args, **kwargs):
        pass


class StandardAudioPreprocessor(WavPreprocessor):

    def __init__(self, audio_config_yaml_path):
        super(StandardAudioPreprocessor, self).__init__(audio_config_yaml_path)

    def trim_long_silences(self, wav):
        """
        Ensures that segments without voice in the waveform remain no longer than a
        threshold determined by the VAD parameters in params.py.
        :param wav: the raw waveform as a numpy array of floats
        :return: the same waveform with silences trimmed away (length <= original wav length)
        """
        int16_max = (2 ** 15) - 1
        vad_window_length = self.audio_config_yaml["VAD_WINDOW_LENGTH"]
        sampling_rate = self.audio_config_yaml["SAMPLING_RATE"]
        vad_moving_average_width = self.audio_config_yaml["VAD_MOVING_AVERAGE_WIDTH"]
        vad_max_silence_length = self.audio_config_yaml["VAD_MAX_SILENCE_LENGTH"]
        # Compute the voice detection window size

        samples_per_window = (vad_window_length * sampling_rate) // 1000

        # Trim the end of the audio to have a multiple of the window size
        wav = wav[:len(wav) - (len(wav) % samples_per_window)]

        # Convert the float waveform to 16-bit mono PCM
        pcm_wave = struct.pack("%dh" % len(wav), *(np.round(wav * int16_max)).astype(np.int16))

        # Perform voice activation detection
        voice_flags = []
        vad = webrtcvad.Vad(mode=3)
        for window_start in range(0, len(wav), samples_per_window):
            window_end = window_start + samples_per_window
            voice_flags.append(vad.is_speech(pcm_wave[window_start * 2:window_end * 2],
                                             sample_rate=sampling_rate))
        voice_flags = np.array(voice_flags)

        # Smooth the voice detection with a moving average
        def moving_average(array, width):
            array_padded = np.concatenate((np.zeros((width - 1) // 2), array, np.zeros(width // 2)))
            ret = np.cumsum(array_padded, dtype=float)
            ret[width:] = ret[width:] - ret[:-width]
            return ret[width - 1:] / width

        audio_mask = moving_average(voice_flags, vad_moving_average_width)
        audio_mask = np.round(audio_mask).astype(np.bool)

        # Dilate the voiced regions
        audio_mask = binary_dilation(audio_mask, np.ones(vad_max_silence_length + 1))
        audio_mask = np.repeat(audio_mask, samples_per_window)

        return wav[audio_mask == True]

    def normalize_volume(self, wav, target_dBFS, increase_only=False, decrease_only=False):
        if increase_only and decrease_only:
            raise ValueError("Both increase only and decrease only are set")
        dBFS_change = target_dBFS - 10 * np.log10(np.mean(wav ** 2))
        if (dBFS_change < 0 and increase_only) or (dBFS_change > 0 and decrease_only):
            return wav
        return wav * (10 ** (dBFS_change / 20))

    def preprocess_wav(self, fpath_or_wav,
                       source_sr=None,
                       normalize=True,
                       trim_silence=True):
        """
        Applies the preprocessing operations used in training the Speaker Encoder to a waveform
        either on disk or in memory. The waveform will be resampled to match the data hyperparameters.
        :param fpath_or_wav: either a filepath to an audio file (many extensions are supported, not
        just .wav), either the waveform as a numpy array of floats.
        :param source_sr: if passing an audio waveform, the sampling rate of the waveform before
        preprocessing. After preprocessing, the waveform's sampling rate will match the data
        hyperparameters. If passing a filepath, the sampling rate will be automatically detected and
        this argument will be ignored.
        """

        sampling_rate = self.audio_config_yaml["SAMPLING_RATE"]
        audio_norm_target_dBFS = self.audio_config_yaml["AUDIO_NORM_TARGET_dBFS"]
        # Load the wav from disk if needed

        if isinstance(fpath_or_wav, str) or isinstance(fpath_or_wav, Path):
            wav, source_sr = librosa.load(str(fpath_or_wav), sr=None)
        else:
            wav = fpath_or_wav

        # Resample the wav if needed
        if source_sr is not None and source_sr != sampling_rate:
            wav = librosa.resample(wav, source_sr, sampling_rate)

        # Apply the preprocessing: normalize volume and shorten long silences
        if normalize:
            wav = self.normalize_volume(wav, audio_norm_target_dBFS, increase_only=True)

        if webrtcvad and trim_silence:
            wav = self.trim_long_silences(wav)

        return wav