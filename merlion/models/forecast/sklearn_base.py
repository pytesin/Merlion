#
# Copyright (c) 2022 salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
#
"""
Base class for forecasters which use arbitrary ``sklearn`` regression models internally.
"""
import logging
from typing import List, Tuple

import numpy as np
import pandas as pd

from merlion.models.forecast.base import ForecasterConfig, ForecasterBase
from merlion.utils.time_series import to_pd_datetime, TimeSeries
from merlion.models.utils.rolling_window_dataset import RollingWindowDataset

logger = logging.getLogger(__name__)


class SKLearnForecasterConfig(ForecasterConfig):
    """
    Configuration class for a `SKLearnForecaster`.
    """

    def __init__(
        self,
        maxlags: int,
        max_forecast_steps: int = None,
        target_seq_index: int = None,
        prediction_stride: int = 1,
        **kwargs,
    ):
        """
        :param maxlags: Max # of lags for forecasting
        :param max_forecast_steps: Max # of steps we would like to forecast for.
        :param target_seq_index: The index of the univariate (amongst all
            univariates in a general multivariate time series) whose value we
            would like to forecast.
        :param prediction_stride: the number of steps being forecasted in a single call to underlying the model

            - If univariate: the sequence target of the length of prediction_stride will be utilized, forecasting will
              be done autoregressively, with the stride unit of prediction_stride
            - If multivariate:

                - if = 1: autoregressively forecast all variables in the time series, one step at a time
                - if > 1: only support directly forecasting the next prediction_stride steps in the future.
                  Autoregression not supported. Note that the model will set prediction_stride = max_forecast_steps.
        """
        super().__init__(max_forecast_steps=max_forecast_steps, target_seq_index=target_seq_index, **kwargs)
        self.maxlags = maxlags
        self.prediction_stride = prediction_stride


class SKLearnForecaster(ForecasterBase):
    """
    Wrapper around a sklearn-style model for time series forecasting. The underlying model must support
    ``fit()`` and ``predict()`` methods. The model can be trained to be either an autoregressive model of order
    ``maxlags``, or to directly predict the next ``prediction_stride`` timestamps from a history of length ``maxlags``.

    If the data is univariate, the model will predict the next ``prediction_stride`` elements of the time series.
    It can then use these predictions to autoregressively predict the next ``prediction_stride`` elements. If the data
    is multivariate, the model will either autoregressively predict the next timestamp of all univariates
    (if ``prediction_stride = 1``), or it will directly predict the next ``prediction_stride`` timestamps of the target
    univariate (if ``prediction_stride > 1``).
    """

    config_class = SKLearnForecasterConfig
    model = None

    def __init__(self, config: SKLearnForecasterConfig):
        super().__init__(config)

    @property
    def maxlags(self) -> int:
        return self.config.maxlags

    @property
    def prediction_stride(self) -> int:
        return self.config.prediction_stride

    @property
    def require_even_sampling(self) -> bool:
        return True

    @property
    def require_univariate(self) -> bool:
        return False

    @property
    def _default_train_config(self):
        return dict()

    def _train(self, train_data: pd.DataFrame, train_config=None):
        fit = train_config.get("fit", True)
        max_forecast_steps = len(train_data) - self.maxlags
        if fit and self.prediction_stride > 1:  # sanity checks for seq2seq prediction
            if self.max_forecast_steps is not None and self.max_forecast_steps > max_forecast_steps:
                logger.warning(
                    f"With train data of length {len(train_data)} and  maxlags={self.maxlags}, the maximum supported "
                    f"forecast steps is {max_forecast_steps}, but got max_forecast_steps={self.max_forecast_steps}. "
                    f"Reducing to the maximum permissible value."
                )
                self.config.max_forecast_steps = max_forecast_steps
            if (
                self.max_forecast_steps is not None
                and self.dim > 1
                and self.prediction_stride != self.max_forecast_steps
            ):
                logger.warning(
                    f"For multivariate dataset, reset prediction_stride = max_forecast_steps = {self.max_forecast_steps}"
                )
                self.config.prediction_stride = self.max_forecast_steps

        if self.dim == 1:
            logger.info(
                f"Model is working on a univariate dataset, hybrid of sequence and autoregression training strategy "
                f"will be adopted with prediction_stride = {self.prediction_stride}."
            )
            data_target_idx = self.target_seq_index
        elif self.prediction_stride == 1:
            logger.info(
                f"Model is working on a multivariate dataset with prediction_stride = 1, model will be trained to "
                f"autoregressively predict all univariates."
            )
            data_target_idx = None
        else:
            logger.info(
                f"Model is working on a multivariate dataset with prediction_stride > 1. Model will directly forecast "
                f"the target univariate for the next {self.prediction_stride} timestamps."
            )
            data_target_idx = self.target_seq_index

        # process train data to the rolling window dataset
        dataset = RollingWindowDataset(
            data=train_data,
            target_seq_index=data_target_idx,
            n_past=self.maxlags,
            n_future=self.prediction_stride,
            batch_size=None,
            ts_index=False,
        )
        inputs_train, inputs_train_ts, labels_train, labels_train_ts = next(iter(dataset))

        # TODO: allow model to use timestamps
        # fitting
        if fit:
            self.model.fit(inputs_train, labels_train)

        # forecasting
        if self.dim == 1:
            pred = self._hybrid_forecast(inputs_train, self.max_forecast_steps or len(inputs_train) - self.maxlags)
        elif self.prediction_stride == 1:
            pred = self._autoregressive_forecast(inputs_train, max(self.max_forecast_steps or 0, 1))
        else:
            pred = self.model.predict(inputs_train)

        # since the model may predict multiple steps, we concatenate all the first steps together
        return pd.DataFrame(pred[:, 0], index=labels_train_ts[:, 0], columns=[self.target_name]), None

    def _forecast(
        self, time_stamps: List[int], time_series_prev: pd.DataFrame = None, return_prev=False
    ) -> Tuple[pd.DataFrame, None]:
        if time_series_prev is not None:
            assert len(time_series_prev) >= self.maxlags, (
                f"time_series_prev has a data length of "
                f"{len(time_series_prev)} that is shorter than the maxlags "
                f"for the model"
            )

        n = len(time_stamps)
        prev_pred, prev_err = None, None
        if time_series_prev is None:
            time_series_prev = self.transform(self.train_data)
        elif time_series_prev is not None and return_prev:
            prev_pred, prev_err = self._train(time_series_prev, train_config=dict(fit=False))

        time_series_prev_no_ts = self._get_immedidate_forecasting_prior(time_series_prev)

        # TODO: allow model to use timestamps
        if self.dim == 1:
            yhat = self._hybrid_forecast(np.atleast_2d(time_series_prev_no_ts), n).reshape(-1)
        elif self.prediction_stride == 1:
            yhat = self._autoregressive_forecast(time_series_prev_no_ts, n).reshape(-1)
        else:
            yhat = self.model.predict(np.atleast_2d(time_series_prev_no_ts)).reshape(-1)[:n]

        forecast = pd.DataFrame(yhat, index=to_pd_datetime(time_stamps), columns=[self.target_name])
        if prev_pred is not None:
            forecast = pd.concat((prev_pred, forecast))
        return forecast, None

    def _hybrid_forecast(self, inputs, steps=None):
        """
        n-step autoregression method for univairate data, each regression step updates n_prediction_steps data points
        :return: pred of target_seq_index for steps [n_samples, steps]
        """
        # TODO: allow model to use timestamps
        if steps is None:
            steps = self.max_forecast_steps

        inputs = np.atleast_2d(inputs)

        pred = np.empty((len(inputs), (int((steps - 1) / self.prediction_stride) + 1) * self.prediction_stride))
        start = 0
        while True:
            next_forecast = self.model.predict(inputs)
            if len(next_forecast.shape) == 1:
                next_forecast = np.expand_dims(next_forecast, axis=1)
            pred[:, start : start + self.prediction_stride] = next_forecast
            start += self.prediction_stride
            if start >= steps:
                break
            inputs = self._update_prior(inputs, next_forecast, for_univariate=True)
        return pred[:, :steps]

    def _autoregressive_forecast(self, inputs, steps=None):
        """
        1-step auto-regression method for multivariate data, each regression step updates one data point for each sequence
        :return: pred of target_seq_index for steps [n_samples, steps]
        """
        # TODO: allow model to use timestamps
        if steps is None:
            steps = self.max_forecast_steps

        inputs = np.atleast_2d(inputs)

        pred = np.empty((len(inputs), steps))

        for i in range(steps):
            # next forecast shape: [n_samples, self.dim]
            next_forecast = self.model.predict(inputs)
            pred[:, i] = next_forecast[:, self.target_seq_index]
            if i == steps - 1:
                break
            inputs = self._update_prior(inputs, next_forecast, for_univariate=False)
        return pred

    def _update_prior(self, prior: np.ndarray, next_forecast: np.ndarray, for_univariate: bool = False):
        """
        regressively update the prior by concatenate prior with next_forecast,
        :param prior:
            if univariate: shape=[n_samples, maxlags]
            if multivariate: shape=[n_samples, n_seq * maxlags]
        :param next_forecast: the next forecasting result
            if univariate: shape=[n_samples, n_prediction_steps],
                if n_prediciton_steps ==1, maybe [n_samples,]
            if multivariate: shape=[n_samples, n_seq]
        :return: updated prior
        """
        # unsqueeze the sequence dimension so prior and next_forecast can be concatenated along sequence dimension
        # for example,
        # prior = [[1,2,3,4,5,6,7,8,9], [10,20,30,40,50,60,70,80,90]], after the sequence dimension is expanded
        # prior = [[[1,2,3], [4,5,6], [7,8,9]],
        #          [[10,20,30],[40,50,60],[70,80,90]]
        #         ]
        # next_forcast = [[0.1,0.2,0.3],[0.4,0.5,0.6]], after the sequence dimension is expanded
        # next_forecast = [[[0.1],[0.2],[0.3]],
        #                  [[0.4],[0.5],[0.6]]
        #                 ]

        assert isinstance(prior, np.ndarray) and len(prior.shape) == 2
        assert isinstance(next_forecast, np.ndarray)
        if for_univariate:
            if len(next_forecast.shape) == 1:
                next_forecast = np.expand_dims(next_forecast, axis=1)
            prior = np.concatenate([prior, next_forecast], axis=1)[:, -self.maxlags :]
        else:
            assert len(next_forecast.shape) == 2
            prior = prior.reshape(len(prior), self.dim, -1)
            next_forecast = np.expand_dims(next_forecast, axis=2)
            prior = np.concatenate([prior, next_forecast], axis=2)[:, :, -self.maxlags :]
        return prior.reshape(len(prior), -1)

    def _get_immedidate_forecasting_prior(self, data):
        if isinstance(data, TimeSeries):
            data = data.to_pd()
        data = data.values
        return data[-self.maxlags :].reshape(-1, order="F")
