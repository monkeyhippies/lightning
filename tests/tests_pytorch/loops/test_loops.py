# Copyright The Lightning AI team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
from copy import deepcopy
from dataclasses import dataclass
from typing import Dict, Iterator
from unittest.mock import ANY, Mock

import pytest
import torch
from torch.utils.data.dataloader import _MultiProcessingDataLoaderIter, DataLoader

from lightning.pytorch import LightningModule, Trainer
from lightning.pytorch.callbacks import Callback, ModelCheckpoint, OnExceptionCheckpoint
from lightning.pytorch.demos.boring_classes import BoringModel, RandomDataset
from lightning.pytorch.loops import _Loop
from lightning.pytorch.loops.progress import _BaseProgress


def test_restarting_loops_recursive():
    class MyLoop(_Loop):
        def __init__(self, loop=None):
            super().__init__(Mock())
            self.child = loop

    loop = MyLoop(MyLoop(MyLoop()))

    assert not loop.restarting
    assert not loop.child.restarting
    assert not loop.child.child.restarting
    loop.restarting = True
    assert loop.restarting
    assert loop.child.restarting
    assert loop.child.child.restarting


class CustomException(Exception):
    pass


def test_loop_restore():
    class Simple(_Loop):
        def __init__(self, trainer, dataset: Iterator):
            super().__init__(trainer)
            self.iteration_count = 0
            self.dataset = dataset

        def run(self):
            self.reset()
            while not self.iteration_count > len(self.dataset):
                try:
                    self.advance()
                    self.iteration_count += 1
                    self._restarting = False
                except StopIteration:
                    break
            self._restarting = False

        def reset(self) -> None:
            self.iter_dataset = iter(self.dataset)
            if self.restarting:
                for _ in range(self.iteration_count):
                    next(self.iter_dataset)
                self.iteration_count += 1
            else:
                self.outputs = []

        def advance(self) -> None:
            value = next(self.iter_dataset)

            if self.iteration_count == 5:
                raise CustomException

            self.outputs.append(value)

        def state_dict(self) -> Dict:
            return {"iteration_count": self.iteration_count, "outputs": self.outputs}

        def load_state_dict(self, state_dict: Dict) -> None:
            self.iteration_count = state_dict["iteration_count"]
            self.outputs = state_dict["outputs"]

    trainer = Trainer()

    data = range(10)
    loop = Simple(trainer, data)
    try:
        loop.run()
        state_dict = {}
    except CustomException:
        state_dict = loop.state_dict()

    loop = Simple(trainer, data)
    loop.load_state_dict(state_dict)
    loop.restarting = True
    loop.run()

    assert not loop.restarting
    assert loop.outputs == list(range(10))


def test_loop_hierarchy():
    @dataclass
    class SimpleProgress(_BaseProgress):
        increment: int = 0

    class Simple(_Loop):
        def __init__(self, trainer, a):
            super().__init__(trainer)
            self.a = a
            self.progress = SimpleProgress()

        def run(self):
            while not self.progress.increment > 0:
                try:
                    self.advance()
                    self.progress.increment += 1
                    self._restarting = False
                except StopIteration:
                    break
            self._restarting = False

        def advance(self) -> None:
            loop = getattr(self, "loop_child", None)
            if not loop:
                return
            loop.run()

        def on_save_checkpoint(self) -> Dict:
            return {"a": self.a}

        def on_load_checkpoint(self, state_dict: Dict) -> None:
            self.a = state_dict["a"]

    trainer = Trainer()
    loop_parent = Simple(trainer, 1)
    loop_child = Simple(trainer, 2)
    loop_parent.loop_child = loop_child

    state_dict = loop_parent.state_dict()
    assert state_dict == {
        "state_dict": {"a": 1},
        "progress": {"increment": 0},
        "loop_child.state_dict": {"a": 2},
        "loop_child.progress": {"increment": 0},
    }

    state_dict["loop_child.state_dict"]["a"] = 3
    # check restarting after `load_state_dict`
    loop_parent.load_state_dict(state_dict)
    assert loop_parent.restarting

    loop_parent.run()

    # check the new state after `run`
    state_dict = loop_parent.state_dict()
    assert state_dict == {
        "state_dict": {"a": 1},
        "progress": {"increment": 1},
        "loop_child.state_dict": {"a": 3},
        "loop_child.progress": {"increment": 1},
    }

    loop_parent_copy = deepcopy(loop_parent)
    assert loop_parent_copy.state_dict() == loop_parent.state_dict()

    assert loop_parent_copy.on_save_checkpoint() == state_dict["state_dict"]
    assert loop_parent_copy.loop_child.on_save_checkpoint() == state_dict["loop_child.state_dict"]

    loop_parent = Simple(trainer, 1)
    loop_child = Simple(trainer, 2)
    loop_parent.loop_child = loop_child
    loop_parent.load_state_dict(state_dict)
    assert loop_parent.progress.increment == 1
    assert loop_parent.loop_child.progress.increment == 1

    del loop_parent.loop_child
    state_dict = loop_parent.state_dict()
    assert state_dict == {"state_dict": {"a": 1}, "progress": {"increment": 1}}


@pytest.mark.parametrize("stop_epoch", (1, 2))
@pytest.mark.parametrize("stop_batch", (1, 2))
@pytest.mark.parametrize("n_dataloaders,stop_dataloader", [(2, 0), (2, 1), (3, 2)])
def test_loop_restart_progress_multiple_dataloaders(tmpdir, n_dataloaders, stop_dataloader, stop_epoch, stop_batch):
    n_batches = 5
    n_epochs = 3

    class ValidationModel(BoringModel):
        def __init__(self):
            super().__init__()

        def validation_step(self, batch, batch_idx, dataloader_idx):
            if self.current_epoch == stop_epoch and batch_idx == stop_batch and dataloader_idx == stop_dataloader:
                raise CustomException
            return super().validation_step(batch, batch_idx)

        def val_dataloader(self):
            return [super(ValidationModel, self).val_dataloader() for _ in range(n_dataloaders)]

    model = ValidationModel()

    trainer = Trainer(
        default_root_dir=tmpdir,
        max_epochs=n_epochs,
        limit_train_batches=1,
        limit_val_batches=n_batches,
        callbacks=OnExceptionCheckpoint(tmpdir),
    )

    # simulate a failure
    with pytest.raises(CustomException):
        trainer.fit(model)

    ckpt_path = str(tmpdir / "on_exception.ckpt")
    checkpoint = torch.load(ckpt_path)["loops"]["fit_loop"]

    trainer.fit_loop.load_state_dict(checkpoint)

    # `nbe_`: non-breaking epoch, as in, no exception will be raised. `be_`: breaking epoch
    # the fit-validation total batch progress is reset per epoch so it's not counted for the total value.
    nbe_total_val_batch = 0  # stop_epoch * n_dataloaders * n_batches
    be_total_val_batch = stop_dataloader * n_batches + stop_batch
    total_val_batch = nbe_total_val_batch + be_total_val_batch
    expected = {
        "total": {
            "ready": total_val_batch + 1,
            "started": total_val_batch + 1,
            "processed": total_val_batch,
            "completed": total_val_batch,
        },
        "current": {
            "ready": total_val_batch + 1,
            "started": total_val_batch + 1,
            "processed": total_val_batch,
            "completed": total_val_batch,
        },
        "is_last_batch": False,
    }
    assert trainer.fit_loop.epoch_loop.val_loop.batch_progress.state_dict() == expected


@pytest.mark.parametrize("accumulate_grad_batches", (1, 2, 3))
@pytest.mark.parametrize("stop_epoch", (1, 2))
@pytest.mark.parametrize("stop_batch", (1, 2))
def test_loop_state_on_exception(accumulate_grad_batches, stop_epoch, stop_batch, tmpdir):
    n_epochs = 3
    n_batches = 3

    class TestModel(BoringModel):
        def training_step(self, batch, batch_idx):
            if self.trainer.current_epoch == stop_epoch and batch_idx == stop_batch:
                raise CustomException
            return super().training_step(batch, batch_idx)

    model = TestModel()

    trainer = Trainer(
        default_root_dir=tmpdir,
        max_epochs=n_epochs,
        limit_train_batches=n_batches,
        limit_val_batches=0,
        accumulate_grad_batches=accumulate_grad_batches,
        enable_progress_bar=False,
        logger=False,
        callbacks=OnExceptionCheckpoint(tmpdir),
    )

    # simulate a failure
    with pytest.raises(CustomException):
        trainer.fit(model)

    ckpt_path = str(tmpdir / "on_exception.ckpt")
    assert os.path.exists(ckpt_path)
    checkpoint = torch.load(ckpt_path)

    optim_progress = trainer.fit_loop.epoch_loop.automatic_optimization.optim_progress
    sch_progress = trainer.fit_loop.epoch_loop.scheduler_progress

    # `nbe_`: non-breaking epoch, as in, no exception will be raised. `be_`: breaking epoch
    nbe_batches_completed = stop_epoch * n_batches
    be_batches_completed = stop_batch
    # lightning applies leftover accumulated gradients when the epoch ends
    has_leftover_accumulation_batches = n_batches % accumulate_grad_batches != 0
    # number of batches that will call `optimizer.step()` during non-breaking and breaking epochs
    nbe_stepping_batches = nbe_batches_completed // accumulate_grad_batches
    be_stepping_batches = be_batches_completed // accumulate_grad_batches

    nbe_total_opt_steps = nbe_stepping_batches + has_leftover_accumulation_batches
    be_total_opt_steps = be_stepping_batches
    assert optim_progress.optimizer_steps == nbe_total_opt_steps + be_total_opt_steps
    assert optim_progress.optimizer.step.current.completed == be_total_opt_steps
    has_opt_stepped_in_be = stop_batch + 1 >= accumulate_grad_batches

    nbe_total_zero_grad = nbe_stepping_batches + has_leftover_accumulation_batches
    # `max` because the first batch always zero-grads
    be_total_zero_grad = max(1, be_stepping_batches)
    assert optim_progress.optimizer.zero_grad.total.completed == nbe_total_zero_grad + be_total_zero_grad
    assert optim_progress.optimizer.zero_grad.current.completed == be_total_zero_grad

    nbe_sch_steps = stop_epoch
    be_sch_steps = 0  # the current epoch did not complete
    assert sch_progress.total.completed == nbe_sch_steps + be_sch_steps
    assert sch_progress.current.completed == be_sch_steps

    expected = {
        "state_dict": ANY,
        "epoch_progress": {
            "total": {
                "ready": stop_epoch + 1,
                "started": stop_epoch + 1,
                "processed": stop_epoch,
                "completed": stop_epoch,
            },
            "current": {
                "ready": stop_epoch + 1,
                "started": stop_epoch + 1,
                "processed": stop_epoch,
                "completed": stop_epoch,
            },
        },
        "epoch_loop.state_dict": ANY,
        "epoch_loop.batch_progress": {
            "total": {
                "ready": nbe_batches_completed + be_batches_completed + 1,
                "started": nbe_batches_completed + be_batches_completed + 1,
                "processed": nbe_batches_completed + be_batches_completed,
                "completed": nbe_batches_completed + be_batches_completed,
            },
            "current": {
                "ready": stop_batch + 1,
                "started": stop_batch + 1,
                "processed": stop_batch,
                "completed": stop_batch,
            },
            "is_last_batch": False,
        },
        "epoch_loop.scheduler_progress": {
            "total": {"ready": nbe_sch_steps + be_sch_steps, "completed": nbe_sch_steps + be_sch_steps},
            "current": {"ready": be_sch_steps, "completed": be_sch_steps},
        },
        "epoch_loop.manual_optimization.state_dict": ANY,
        "epoch_loop.manual_optimization.optim_step_progress": {
            "total": {"ready": 0, "completed": 0},
            "current": {"ready": 0, "completed": 0},
        },
        "epoch_loop.automatic_optimization.state_dict": {},
        "epoch_loop.automatic_optimization.optim_progress": {
            "optimizer": {
                "step": {
                    "total": {
                        "ready": nbe_total_opt_steps + be_total_opt_steps + has_opt_stepped_in_be,
                        "completed": nbe_total_opt_steps + be_total_opt_steps,
                    },
                    "current": {"ready": be_total_opt_steps + has_opt_stepped_in_be, "completed": be_total_opt_steps},
                },
                "zero_grad": {
                    "total": {
                        "ready": nbe_total_zero_grad + be_total_zero_grad,
                        "started": nbe_total_zero_grad + be_total_zero_grad,
                        "completed": nbe_total_zero_grad + be_total_zero_grad,
                    },
                    "current": {
                        "ready": be_total_zero_grad,
                        "started": be_total_zero_grad,
                        "completed": be_total_zero_grad,
                    },
                },
            },
        },
        "epoch_loop.val_loop.state_dict": ANY,
        "epoch_loop.val_loop.batch_progress": ANY,
    }
    assert checkpoint["loops"]["fit_loop"] == expected

    trainer.fit_loop.load_state_dict(checkpoint["loops"]["fit_loop"])
    state_dict = trainer.fit_loop.state_dict()

    # need to remove these elements for comparison; comparing with `fit_loop.state_dict()` would require the
    # fit loop to have an iterator, which is only available during training
    state_dict["epoch_loop.state_dict"]["dataloader_state_dict"] = ANY
    checkpoint["loops"]["fit_loop"]["epoch_loop.state_dict"]["dataloader_state_dict"] = ANY
    assert state_dict == checkpoint["loops"]["fit_loop"]

    trainer.fit_loop.load_state_dict(checkpoint["loops"]["fit_loop"])
    # test resetting manually, we expect all `ready` counters to be reset to `completed`
    trainer.fit_loop.reset()
    trainer.fit_loop.epoch_loop.reset()

    epoch_progress = trainer.fit_loop.epoch_progress
    assert epoch_progress.current.ready == stop_epoch
    assert epoch_progress.current.completed == stop_epoch

    batch_progress = trainer.fit_loop.epoch_loop.batch_progress
    assert batch_progress.current.ready == be_batches_completed
    assert batch_progress.current.completed == be_batches_completed

    optim_progress = trainer.fit_loop.epoch_loop.automatic_optimization.optim_progress
    assert optim_progress.optimizer.step.current.ready == be_total_opt_steps
    assert optim_progress.optimizer.step.current.completed == be_total_opt_steps
    assert optim_progress.optimizer.zero_grad.current.ready == be_total_zero_grad
    assert optim_progress.optimizer.zero_grad.current.completed == be_total_zero_grad

    state_dict = trainer.fit_loop.state_dict()
    assert state_dict != checkpoint["loops"]["fit_loop"]
    assert state_dict["epoch_progress"]["total"]["started"] == stop_epoch + 1
    assert state_dict["epoch_progress"]["current"]["started"] == stop_epoch


def test_loop_state_on_complete_run(tmpdir):
    n_epochs = 3
    n_batches = 3
    accumulate_grad_batches = 1

    class TestModel(BoringModel):
        def train_dataloader(self):
            # override to test the `is_last_batch` value
            return DataLoader(RandomDataset(32, n_batches))

    model = TestModel()

    trainer = Trainer(
        default_root_dir=tmpdir,
        max_epochs=n_epochs,
        limit_val_batches=0,
        accumulate_grad_batches=accumulate_grad_batches,
        enable_progress_bar=False,
        logger=False,
    )
    trainer.fit(model)

    assert trainer.num_training_batches == n_batches

    ckpt_path = trainer.checkpoint_callback.best_model_path
    assert os.path.exists(ckpt_path)
    checkpoint = torch.load(ckpt_path)

    n_sch_steps_total = n_epochs
    n_sch_steps_current = 1

    expected = {
        "state_dict": ANY,
        "epoch_progress": {
            "total": {
                "ready": n_epochs,
                "started": n_epochs,
                "processed": n_epochs,
                "completed": n_epochs - 1,
            },
            "current": {
                "ready": n_epochs,
                "started": n_epochs,
                "processed": n_epochs,
                "completed": n_epochs - 1,
            },
        },
        "epoch_loop.state_dict": ANY,
        "epoch_loop.batch_progress": {
            "total": {
                "ready": n_epochs * n_batches,
                "started": n_epochs * n_batches,
                "processed": n_epochs * n_batches,
                "completed": n_epochs * n_batches,
            },
            "current": {
                "ready": n_batches,
                "started": n_batches,
                "processed": n_batches,
                "completed": n_batches,
            },
            "is_last_batch": True,
        },
        "epoch_loop.scheduler_progress": {
            "total": {"ready": n_sch_steps_total, "completed": n_sch_steps_total},
            "current": {"ready": n_sch_steps_current, "completed": n_sch_steps_current},
        },
        "epoch_loop.manual_optimization.state_dict": ANY,
        "epoch_loop.manual_optimization.optim_step_progress": {
            "total": {"ready": 0, "completed": 0},
            "current": {"ready": 0, "completed": 0},
        },
        "epoch_loop.automatic_optimization.state_dict": {},
        "epoch_loop.automatic_optimization.optim_progress": {
            "optimizer": {
                "step": {
                    "total": {
                        "ready": n_epochs * n_batches,
                        "completed": n_epochs * n_batches,
                    },
                    "current": {
                        "ready": n_batches,
                        "completed": n_batches,
                    },
                },
                "zero_grad": {
                    "total": {
                        "ready": n_epochs * n_batches,
                        "started": n_epochs * n_batches,
                        "completed": n_epochs * n_batches,
                    },
                    "current": {
                        "ready": n_batches,
                        "started": n_batches,
                        "completed": n_batches,
                    },
                },
            },
        },
        "epoch_loop.val_loop.state_dict": ANY,
        "epoch_loop.val_loop.batch_progress": ANY,
    }
    assert checkpoint["loops"]["fit_loop"] == expected


def test_fit_loop_reset(tmpdir):
    """Test that the reset logic in fit- and epoch loop is aware of whether the loop is restarting from a completed
    loop or from a mid-epoch checkpoint."""

    # generate checkpoints at end of epoch and mid-epoch
    model = BoringModel()
    checkpoint_callback = ModelCheckpoint(
        dirpath=tmpdir,
        every_n_train_steps=2,
        save_top_k=-1,
    )
    trainer = Trainer(
        default_root_dir=tmpdir,
        limit_train_batches=4,
        max_epochs=2,
        callbacks=[checkpoint_callback],
        logger=False,
        enable_model_summary=False,
    )
    trainer.fit(model)

    # reset state loaded from a checkpoint from mid-epoch
    mid_epoch_ckpt = torch.load(str(tmpdir / "epoch=0-step=2.ckpt"))
    fit_loop = trainer.fit_loop
    epoch_loop = fit_loop.epoch_loop
    optimizer_loop = epoch_loop.automatic_optimization
    assert not fit_loop.restarting
    assert not epoch_loop.restarting
    assert not optimizer_loop.restarting

    # we load exactly what was saved - no reset yet
    fit_loop.load_state_dict(mid_epoch_ckpt["loops"]["fit_loop"])
    # resetting from a mid-of-epoch checkpoint SHOULD NOT reset the current counters to 0
    fit_loop.reset()
    epoch_loop.reset()

    assert fit_loop.restarting
    assert fit_loop.epoch_progress.total.ready == 1
    assert fit_loop.epoch_progress.total.completed == 0  # the checkpoint was saved mid epoch
    assert fit_loop.epoch_progress.current.ready == 0
    assert fit_loop.epoch_progress.current.completed == 0

    assert epoch_loop.restarting
    assert epoch_loop.batch_progress.total.ready == 2
    assert epoch_loop.batch_progress.total.processed == 2
    assert epoch_loop.batch_progress.total.completed == 1  # the checkpoint was saved on train_batch_end
    assert epoch_loop.batch_progress.current.ready == 1  # currents get set to the completed value
    assert epoch_loop.batch_progress.current.processed == 1
    assert epoch_loop.batch_progress.current.completed == 1

    assert optimizer_loop.restarting

    # reset state loaded from a checkpoint from the end of an epoch
    end_of_epoch_ckpt = torch.load(str(tmpdir / "epoch=0-step=4.ckpt"))
    fit_loop = trainer.fit_loop
    epoch_loop = fit_loop.epoch_loop
    fit_loop.restarting = False
    epoch_loop.restarting = False
    optimizer_loop.restarting = False

    # we load exactly what was saved - no reset yet
    fit_loop.load_state_dict(end_of_epoch_ckpt["loops"]["fit_loop"])
    # resetting from a end-of-epoch checkpoint SHOULD reset the current counters to 0
    fit_loop.reset()
    epoch_loop.reset()

    assert fit_loop.restarting
    assert fit_loop.epoch_progress.total.ready == 1
    assert fit_loop.epoch_progress.total.completed == 0  # the checkpoint saves before the epoch completes
    assert fit_loop.epoch_progress.current.ready == 0
    assert fit_loop.epoch_progress.current.completed == 0

    assert epoch_loop.restarting
    assert epoch_loop.batch_progress.total.ready == 4
    assert epoch_loop.batch_progress.total.processed == 4
    assert epoch_loop.batch_progress.total.completed == 3  # the checkpoint was saved on train_batch_end
    assert epoch_loop.batch_progress.current.ready == 3  # currents get set to the completed value
    assert epoch_loop.batch_progress.current.processed == 3
    assert epoch_loop.batch_progress.current.completed == 3


@pytest.mark.parametrize(
    ["train_datasets", "val_datasets"],
    [([RandomDataset], [RandomDataset]), ([RandomDataset], [RandomDataset, RandomDataset])],
)
@pytest.mark.parametrize("val_check_interval", [0.5, 1.0])
def test_fit_can_fail_during_validation(train_datasets, val_datasets, val_check_interval, tmpdir):
    size, n_batches = 2, 4
    stop_batch = 1
    n_val_dataloaders = len(val_datasets)
    stop_dataloader = n_val_dataloaders - 1

    class TestModel(LightningModule):
        def __init__(self, should_fail):
            super().__init__()
            self.layer = torch.nn.Linear(size, 2)
            self.should_fail = should_fail

        def step(self, batch):
            return sum(self.layer(b).sum() for b in batch)

        def training_step(self, batch, batch_idx):
            return self.step(batch)

        def validation_step(self, batch, batch_idx, dataloader_idx=0):
            if self.should_fail and dataloader_idx == stop_dataloader and batch_idx == stop_batch:
                raise CustomException
            return self.step(batch)

        def configure_optimizers(self):
            return torch.optim.SGD(self.layer.parameters(), lr=0.1)

        def train_dataloader(self):
            return [DataLoader(cls(size, n_batches)) for cls in train_datasets]

        def val_dataloader(self):
            return [DataLoader(cls(size, n_batches)) for cls in val_datasets]

    model = TestModel(False)
    trainer = Trainer(
        default_root_dir=tmpdir,
        max_epochs=1,
        val_check_interval=val_check_interval,
        num_sanity_val_steps=0,
        enable_progress_bar=False,
        callbacks=OnExceptionCheckpoint(tmpdir),
    )
    trainer.fit(model)

    ckpt_path = os.path.join(tmpdir, "on_exception.ckpt")
    assert not os.path.exists(ckpt_path), "Shouldn't have failed"
    state_dict = trainer.fit_loop.state_dict()
    expected_global_step = trainer.global_step

    assert state_dict["epoch_loop.batch_progress"] == {
        "total": {"ready": n_batches, "started": n_batches, "processed": n_batches, "completed": n_batches},
        "current": {"ready": n_batches, "started": n_batches, "processed": n_batches, "completed": n_batches},
        "is_last_batch": True,
    }

    val_per_epoch = int(1 // val_check_interval)
    assert state_dict["epoch_loop.val_loop.batch_progress"] == {
        "total": {
            "ready": n_val_dataloaders * val_per_epoch * n_batches,
            "started": n_val_dataloaders * val_per_epoch * n_batches,
            "processed": n_val_dataloaders * val_per_epoch * n_batches,
            "completed": n_val_dataloaders * val_per_epoch * n_batches,
        },
        "current": {
            "ready": n_val_dataloaders * n_batches,
            "started": n_val_dataloaders * n_batches,
            "processed": n_val_dataloaders * n_batches,
            "completed": n_val_dataloaders * n_batches,
        },
        "is_last_batch": True,
    }

    model = TestModel(True)
    trainer = Trainer(
        default_root_dir=tmpdir,
        max_epochs=1,
        val_check_interval=val_check_interval,
        num_sanity_val_steps=0,
        enable_progress_bar=False,
        callbacks=OnExceptionCheckpoint(tmpdir),
    )
    with pytest.raises(CustomException):
        # will stop during validation
        trainer.fit(model)

    assert os.path.exists(ckpt_path)
    checkpoint = torch.load(ckpt_path)["loops"]["fit_loop"]

    per_val_train_batches = int(n_batches * val_check_interval)
    assert checkpoint["epoch_loop.batch_progress"] == {
        "total": {
            "ready": per_val_train_batches,
            "started": per_val_train_batches,
            "processed": per_val_train_batches,
            "completed": per_val_train_batches,
        },
        "current": {
            "ready": per_val_train_batches,
            "started": per_val_train_batches,
            "processed": per_val_train_batches,
            "completed": per_val_train_batches,
        },
        "is_last_batch": val_check_interval == 1,
    }

    val_batch_progress = "epoch_loop.val_loop.batch_progress"
    # "nb_": non-breaking
    nb_total_val_batch = stop_dataloader * n_batches
    assert checkpoint[val_batch_progress] == {
        "total": {
            "ready": nb_total_val_batch + stop_batch + 1,
            "started": nb_total_val_batch + stop_batch + 1,
            "processed": nb_total_val_batch + stop_batch,
            "completed": nb_total_val_batch + stop_batch,
        },
        "current": {
            "ready": nb_total_val_batch + stop_batch + 1,
            "started": nb_total_val_batch + stop_batch + 1,
            "processed": nb_total_val_batch + stop_batch,
            "completed": nb_total_val_batch + stop_batch,
        },
        "is_last_batch": False,
    }

    model = TestModel(False)
    trainer = Trainer(
        default_root_dir=tmpdir,
        max_epochs=1,
        val_check_interval=val_check_interval,
        enable_progress_bar=False,
    )
    trainer.fit(model, ckpt_path=ckpt_path)

    assert trainer.global_step == expected_global_step

    state_dict_after_restart = trainer.fit_loop.state_dict()

    # should get the same values as in the run that did not fail
    # totals are increased by 1 (the failed batch which never completed)
    expected = state_dict.copy()

    assert state_dict_after_restart["epoch_loop.batch_progress"] == expected["epoch_loop.batch_progress"]

    expected[val_batch_progress]["total"]["ready"] += 1
    expected[val_batch_progress]["total"]["started"] += 1
    assert state_dict_after_restart[val_batch_progress] == expected[val_batch_progress]


@pytest.mark.parametrize("should_fail", [False, True])
@pytest.mark.parametrize("persistent_workers", [False, True])
def test_workers_are_shutdown(tmpdir, should_fail, persistent_workers):
    # `num_workers == 1` uses `_MultiProcessingDataLoaderIter`
    # `persistent_workers` makes sure `self._iterator` gets set on the `DataLoader` instance

    class TestCallback(Callback):
        def on_train_epoch_end(self, trainer, *_):
            if trainer.current_epoch == 1:
                raise CustomException

    max_epochs = 3

    model = BoringModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        limit_train_batches=2,
        limit_val_batches=2,
        max_epochs=max_epochs,
        callbacks=TestCallback() if should_fail else None,
        enable_checkpointing=False,
        enable_model_summary=False,
        enable_progress_bar=False,
        logger=False,
    )

    class _TestMultiProcessingDataLoaderIter(_MultiProcessingDataLoaderIter):
        def __init__(self, *args, dataloader, **kwargs):
            super().__init__(*args, **kwargs)
            self.dataloader = dataloader

        def _shutdown_workers(self):
            self.dataloader.shutdown_workers_epochs.append(trainer.current_epoch)
            super()._shutdown_workers()

    class TestDataLoader(DataLoader):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.shutdown_workers_epochs = []

        def _get_iterator(self):
            if self.num_workers == 0:
                return super()._get_iterator()
            else:
                self.check_worker_number_rationality()
                return _TestMultiProcessingDataLoaderIter(self, dataloader=self)

    train_dataloader = TestDataLoader(RandomDataset(32, 64), num_workers=1, persistent_workers=persistent_workers)
    val_dataloader = TestDataLoader(RandomDataset(32, 64), num_workers=1, persistent_workers=persistent_workers)

    if should_fail:
        with pytest.raises(CustomException):
            trainer.fit(model, train_dataloader, val_dataloader)
    else:
        trainer.fit(model, train_dataloader, val_dataloader)

    if persistent_workers:
        expected = [trainer.current_epoch, trainer.current_epoch]  # once epoch end, once on teardown
    elif should_fail:
        expected = [
            # iterable check
            0,
            # epoch ends
            1,
            # teardown
            1,
        ]
    else:
        expected = [
            # iterable check
            0,
            # epoch ends
            1,
            2,
            # teardown
            3,
        ]
    assert train_dataloader.shutdown_workers_epochs == expected

    if persistent_workers:
        expected = [trainer.current_epoch, trainer.current_epoch]  # once epoch end, once on teardown
    elif should_fail:
        expected = [
            # sanity check
            0,
            # iterable check
            0,
            # epoch ends
            1,
            # teardown
            1,
        ]
    else:
        expected = [
            # sanity check
            0,
            # iterable check
            0,
            # epoch ends
            1,
            2,
            # teardown
            3,
        ]
    assert val_dataloader.shutdown_workers_epochs == expected
