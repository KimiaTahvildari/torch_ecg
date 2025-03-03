"""
"""

import itertools
from copy import deepcopy
from pathlib import Path

import pytest
import torch
from tqdm.auto import tqdm

from torch_ecg.model_configs import ECG_CRNN_CONFIG
from torch_ecg.model_configs.ati_cnn import ATI_CNN_CONFIG
from torch_ecg.models.ecg_crnn import ECG_CRNN, ECG_CRNN_v1

_TMP_DIR = Path(__file__).parents[1] / "tmp"
_TMP_DIR.mkdir(exist_ok=True)
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


@torch.no_grad()
def test_ecg_crnn():
    n_leads = 12
    classes = ["NSR", "AF", "PVC", "LBBB", "RBBB", "PAB", "VFL"]
    inp = torch.randn(2, n_leads, 2000).to(DEVICE)

    grid = itertools.product(
        [cnn_name for cnn_name in ECG_CRNN_CONFIG.cnn.keys() if cnn_name != "name"],
        [rnn_name for rnn_name in ECG_CRNN_CONFIG.rnn.keys() if rnn_name != "name"] + ["none"],
        [attn_name for attn_name in ECG_CRNN_CONFIG.attn.keys() if attn_name != "name"] + ["none"],
        ["none", "max", "avg"],  # global pool
    )
    total = (len(ECG_CRNN_CONFIG.cnn.keys()) - 1) * len(ECG_CRNN_CONFIG.rnn.keys()) * len(ECG_CRNN_CONFIG.attn.keys()) * 3

    for cnn_name, rnn_name, attn_name, global_pool in tqdm(grid, total=total, mininterval=1):
        config = deepcopy(ECG_CRNN_CONFIG)
        config.cnn.name = cnn_name
        config.rnn.name = rnn_name
        config.attn.name = attn_name
        config.global_pool = global_pool

        model = ECG_CRNN(classes=classes, n_leads=n_leads, config=config).to(DEVICE)
        model = model.eval()
        out = model(inp)
        assert out.shape == model.compute_output_shape(seq_len=inp.shape[-1], batch_size=inp.shape[0])

        model_v1 = ECG_CRNN_v1(classes=classes, n_leads=n_leads, config=config).to(DEVICE)
        model_v1 = model_v1.eval()
        out_v1 = model_v1(inp)
        # the `compute_output_shape` method is not fully correct for v1
        # assert out_v1.shape == model_v1.compute_output_shape(
        #     seq_len=inp.shape[-1], batch_size=inp.shape[0]
        # )
        model_v1.compute_output_shape(seq_len=inp.shape[-1], batch_size=inp.shape[0])

        # load weights from v1
        model.cnn.load_state_dict(model_v1.cnn.state_dict())
        if model.rnn.__class__.__name__ != "Identity":
            model.rnn.load_state_dict(model_v1.rnn.state_dict())
        if model.attn.__class__.__name__ != "Identity":
            model.attn.load_state_dict(model_v1.attn.state_dict())
        model.clf.load_state_dict(model_v1.clf.state_dict())

    doi = model.doi
    assert isinstance(doi, list)
    assert all([isinstance(d, str) for d in doi]), doi
    doi = model_v1.doi
    assert isinstance(doi, list)
    assert all([isinstance(d, str) for d in doi]), doi


def test_warns_errors():
    n_leads = 12
    classes = ["NSR", "AF", "PVC", "LBBB", "RBBB", "PAB", "VFL"]
    inp = torch.randn(2, n_leads, 2000).to(DEVICE)

    with pytest.warns(RuntimeWarning, match="No config is provided, using default config"):
        model = ECG_CRNN(classes=classes, n_leads=n_leads).to(DEVICE)
    with pytest.warns(RuntimeWarning, match="No config is provided, using default config"):
        model_v1 = ECG_CRNN_v1(classes=classes, n_leads=n_leads).to(DEVICE)

    with pytest.raises(NotImplementedError, match="Implement a task-specific inference method."):
        model.inference(inp)

    with pytest.raises(NotImplementedError, match="Implement a task-specific inference method."):
        model_v1.inference(inp)

    config = deepcopy(ECG_CRNN_CONFIG)
    config.cnn.name = "not_implemented"
    config.cnn.not_implemented = {}
    with pytest.raises(NotImplementedError, match="CNN \042.+\042 not implemented yet"):
        ECG_CRNN(classes=classes, n_leads=n_leads, config=config)
    with pytest.raises(NotImplementedError, match="CNN \042.+\042 not implemented yet"):
        ECG_CRNN_v1(classes=classes, n_leads=n_leads, config=config)

    config = deepcopy(ECG_CRNN_CONFIG)
    config.rnn.name = "not_implemented"
    config.rnn.not_implemented = {}
    with pytest.raises(NotImplementedError, match="RNN \042.+\042 not implemented yet"):
        ECG_CRNN(classes=classes, n_leads=n_leads, config=config)
    with pytest.raises(NotImplementedError, match="RNN \042.+\042 not implemented yet"):
        ECG_CRNN_v1(classes=classes, n_leads=n_leads, config=config)

    config = deepcopy(ECG_CRNN_CONFIG)
    config.attn.name = "not_implemented"
    config.attn.not_implemented = {}
    with pytest.raises(NotImplementedError, match="Attention \042.+\042 not implemented yet"):
        ECG_CRNN(classes=classes, n_leads=n_leads, config=config)
    with pytest.raises(NotImplementedError, match="Attention \042.+\042 not implemented yet"):
        ECG_CRNN_v1(classes=classes, n_leads=n_leads, config=config)

    config = deepcopy(ECG_CRNN_CONFIG)
    config.global_pool = "not_implemented"
    with pytest.raises(NotImplementedError, match="Global Pooling \042.+\042 not implemented yet"):
        ECG_CRNN(classes=classes, n_leads=n_leads, config=config)
    with pytest.raises(NotImplementedError, match="Global Pooling \042.+\042 not implemented yet"):
        ECG_CRNN_v1(classes=classes, n_leads=n_leads, config=config)

    config = deepcopy(ECG_CRNN_CONFIG)
    config.global_pool = "attn"
    with pytest.raises(NotImplementedError, match="Attentive pooling not implemented yet"):
        ECG_CRNN(classes=classes, n_leads=n_leads, config=config)
    with pytest.raises(NotImplementedError, match="Attentive pooling not implemented yet"):
        ECG_CRNN_v1(classes=classes, n_leads=n_leads, config=config)


def test_from_v1():
    config = deepcopy(ECG_CRNN_CONFIG)
    n_leads = 12
    classes = ["NSR", "AF", "PVC", "LBBB", "RBBB", "PAB", "VFL"]
    model_v1 = ECG_CRNN_v1(classes=classes, n_leads=n_leads, config=config)
    model_v1.save(_TMP_DIR / "ecg_crnn_v1.pth", {"classes": classes, "n_leads": n_leads})
    model = ECG_CRNN.from_v1(_TMP_DIR / "ecg_crnn_v1.pth")
    del model
    model, _ = ECG_CRNN.from_v1(_TMP_DIR / "ecg_crnn_v1.pth", return_config=True)
    (_TMP_DIR / "ecg_crnn_v1.pth").unlink()
    del model_v1, model


def test_ati_cnn():
    n_leads = 12
    classes = ["NSR", "AF", "PVC", "LBBB", "RBBB", "PAB", "VFL"]
    config = deepcopy(ATI_CNN_CONFIG)
    model = ECG_CRNN(classes=classes, n_leads=n_leads, config=config).to(DEVICE)
    inp = torch.randn(2, n_leads, 2000).to(DEVICE)
    out = model(inp)
    assert out.shape == model.compute_output_shape(inp.shape[-1], inp.shape[0])
