"""
Auxiliary Sklearn-compatible classification class
"""

import numbers
import os
os.environ['THEANO_FLAGS'] = "floatX=float32"
from typing import Iterable, Union

import numpy as np
import pandas as pd
import pygam as gam
from sklearn import base as skbase
from sklearn import svm as sksvm
from sklearn.utils import validation as skutilvalid
from statsmodels.nonparametric import kernel_regression as smkernel
from statsmodels.nonparametric import _kernel_base as smkernelbase
from theano import shared
import pymc3 as pm
from pymc3 import math as pmmath
# import xgboost


class _LogisticGAM(gam.LogisticGAM):
    """
    Sklearn-compatible Additive Logistic base classifier
    """

    def __init__(self, lam=0.6, max_iter=100, n_splines=25, spline_order=3,
                 penalties='auto', dtype='auto', tol=1e-4,
                 callbacks=('deviance', 'diffs', 'accuracy'),
                 fit_intercept=True, fit_linear=False, fit_splines=True,
                 constraints=None):
        gam.LogisticGAM.__init__(self, lam=lam, max_iter=max_iter, n_splines=n_splines, spline_order=spline_order,
                                 penalties=penalties, dtype=dtype, tol=tol,
                                 callbacks=callbacks,
                                 fit_intercept=fit_intercept, fit_linear=fit_linear, fit_splines=fit_splines,
                                 constraints=constraints)

    def get_params(self, deep=False):
        params = gam.LogisticGAM.get_params(self, deep=deep)
        del params['verbose']
        return params

    def predict_proba(self, X):
        proba = gam.LogisticGAM.predict_proba(self, X)
        skProba = np.zeros((len(proba), 2), dtype=float)
        skProba[:, 1] = proba
        skProba[:, 0] = 1 - proba
        return skProba


class _LogisticLinearLocal(skbase.BaseEstimator, skbase.ClassifierMixin):
    """
    Sklearn-compatible Local Logistic classifier (using Local Linear as a proxy)
    """

    def __init__(self, reg_type: str = 'll', bw: Union[str, float, Iterable] = 'cv_ls'):
        self.reg_type = reg_type
        self.bw = bw

    def fit(self, X: Union[pd.DataFrame, np.ndarray], y: Union[pd.DataFrame, np.ndarray, Iterable]) \
            -> skbase.ClassifierMixin:
        X, y = self._check_X_y_fit(X, y)
        # self.classes_ = skutilmult.unique_labels(y)
        self.nfeatures_ = X.shape[1]
        bw = np.full(self.nfeatures_, self.bw) if isinstance(self.bw, numbers.Number) else self.bw

        self.model_ = smkernel.KernelReg(endog=y * 2 - 1, exog=X, var_type='c' * self.nfeatures_,
                                         reg_type=self.reg_type, bw=bw,
                                         defaults=smkernelbase.EstimatorSettings(efficient=False))
        return self

    def decision_function(self, X) -> np.ndarray:
        skutilvalid.check_is_fitted(self, ['model_'])
        X = self._check_X_predict(X)
        dsn_pred, mgn_pred = self.model_.fit(data_predict=X)
        return dsn_pred

    def predict(self, X) -> np.ndarray:
        skutilvalid.check_is_fitted(self, ['model_'])
        dsn_pred = self.decision_function(X)
        y_pred = (dsn_pred > 0).astype(int)
        return y_pred

    def predict_proba(self, X) -> np.ndarray:
        skutilvalid.check_is_fitted(self, ['model_'])
        dsn_pred = self.decision_function(X)
        proba_pred = np.zeros((X.shape[0], 2), dtype=np.float)
        proba_pred[:, 1] = 1 / (1 + np.exp(-dsn_pred))
        proba_pred[:, 0] = 1 - proba_pred[:, 0]
        return proba_pred

    def _check_X_y_fit(self, X, y):
        X, y = skutilvalid.check_X_y(X, y)
        assert np.all(np.unique(y) == np.array([0, 1]))
        return X, y

    def _check_X_predict(self, X):
        X = skutilvalid.check_array(X)
        assert X.shape[1] == self.nfeatures_, "Wrong X shape"
        return X

    # def score(self, X, y, sample_weight=None):
    #     pass


class _LogisticBayesian(skbase.BaseEstimator, skbase.ClassifierMixin):
    """
    Sklearn-compatible Bayesian Logistic classifier
    """

    def __init__(self, featuresSd=10, nsamplesFit=200, nsamplesPredict=100, mcmc=True,
                 nsampleTune=200, discardTuned=True, samplerStep=None, samplerInit='auto'):
        self.featuresSd = featuresSd
        self.nsamplesFit = nsamplesFit
        self.nsamplesPredict = nsamplesPredict
        self.mcmc = mcmc
        self.nsampleTune = nsampleTune
        self.discardTuned = discardTuned
        self.samplerStep = samplerStep
        self.samplerInit = samplerInit

    def fit(self, X: Union[pd.DataFrame, np.ndarray], y: Union[pd.DataFrame, np.ndarray, Iterable]) \
            -> skbase.ClassifierMixin:
        X, y = self._check_X_y_fit(X, y)
        self.X_shared_ = shared(X)
        self.y_shared_ = shared(y)
        self.nfeatures_ = X.shape[1]
        self.model_ = pm.Model(name='')
        # self.model_.Var('beta', pm.Normal(mu=0, sd=self.featuresSd))
        with self.model_:
            beta = pm.Normal('beta', mu=0, sd=self.featuresSd, testval=0, shape=self.nfeatures_)
            # mu = pm.Deterministic('mu', var=pmmath.dot(beta, self.X_shared_.T))
            mu = pmmath.dot(beta, self.X_shared_.T)
            y_obs = pm.Bernoulli('y_obs', p=pm.invlogit(mu), observed=self.y_shared_)
            if self.mcmc:
                self.trace_ = pm.sample(draws=self.nsamplesFit, tune=self.nsampleTune,
                                        discard_tuned_samples=self.discardTuned,
                                        step=self.samplerStep, init=self.samplerInit, progressbar=True)
            else:
                approx = pm.fit(method='advi')
                self.trace_ = approx.sample(draws=self.nsamplesFit)
        return self

    def decision_function(self, X) -> np.ndarray:
        skutilvalid.check_is_fitted(self, ['model_'])
        X = self._check_X_predict(X)
        self.X_shared_.set_value(X)
        self.y_shared_.set_value(np.zeros(X.shape[0], dtype=np.int))
        with self.model_:
            post_pred = pm.sample_ppc(trace=self.trace_, samples=self.nsamplesPredict,
                                      progressbar=False)['y_obs'].mean(axis=0)
        return post_pred

    def predict(self, X) -> np.ndarray:
        skutilvalid.check_is_fitted(self, ['model_'])
        dsn_pred = self.decision_function(X)
        y_pred = (dsn_pred > 0.5).astype(int)
        return y_pred

    def predict_proba(self, X) -> np.ndarray:
        skutilvalid.check_is_fitted(self, ['model_'])
        dsn_pred = self.decision_function(X)
        proba_pred = np.zeros((X.shape[0], 2), dtype=np.float)
        proba_pred[:, 1] = dsn_pred
        proba_pred[:, 0] = 1 - proba_pred[:, 0]
        return proba_pred

    def _check_X_y_fit(self, X, y):
        X, y = skutilvalid.check_X_y(X, y)
        assert np.all(np.unique(y) == np.array([0, 1]))
        return X, y

    def _check_X_predict(self, X):
        X = skutilvalid.check_array(X)
        assert X.shape[1] == self.nfeatures_, "Wrong X shape"
        return X


class _SVM(sksvm.SVC):
    """
    Base Sklearn SVM classifer with a faster (but very approximate) predict_proba function
    """

    def predict_proba(self, X) -> np.ndarray:
        dsn_pred = self.decision_function(X)
        proba_pred = np.zeros((X.shape[0], 2), dtype=np.float)
        proba_pred[:, 1] = 1 / (1 + np.exp(-dsn_pred))
        proba_pred[:, 0] = 1 - proba_pred[:, 0]
        return proba_pred