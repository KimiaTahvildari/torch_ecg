"""
PreProcessors as torch.nn.Module
"""

from .bandpass import BandPass
from .baseline_remove import BaselineRemove
from .normalize import (
    Normalize,
    MinMaxNormalize,
    NaiveNormalize,
    ZScoreNormalize,
)
from .resample import Resample


__all__ = [
    "BandPass",
    "BaselineRemove",
    "Normalize",
    "MinMaxNormalize",
    "NaiveNormalize",
    "ZScoreNormalize",
    "Resample",
]
