#  Licensed to Elasticsearch B.V. under one or more contributor
#  license agreements. See the NOTICE file distributed with
#  this work for additional information regarding copyright
#  ownership. Elasticsearch B.V. licenses this file to you under
#  the Apache License, Version 2.0 (the "License"); you may
#  not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
# 	http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing,
#  software distributed under the License is distributed on an
#  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#  KIND, either express or implied.  See the License for the
#  specific language governing permissions and limitations
#  under the License.

from operator import itemgetter

import numpy as np
import pytest

import eland as ed
from eland.ml import MLModel
from tests import ES_TEST_CLIENT, ES_VERSION, FLIGHTS_SMALL_INDEX_NAME

try:
    from sklearn import datasets
    from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
    from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

try:
    from xgboost import XGBClassifier, XGBRegressor

    HAS_XGBOOST = True
except ImportError:
    HAS_XGBOOST = False

try:
    from lightgbm import LGBMClassifier, LGBMRegressor

    HAS_LIGHTGBM = True
except ImportError:
    HAS_LIGHTGBM = False

try:
    import shap

    HAS_SHAP = True
except ImportError:
    HAS_SHAP = False


requires_sklearn = pytest.mark.skipif(
    not HAS_SKLEARN, reason="This test requires 'scikit-learn' package to run."
)
requires_xgboost = pytest.mark.skipif(
    not HAS_XGBOOST, reason="This test requires 'xgboost' package to run."
)
requires_shap = pytest.mark.skipif(
    not HAS_SHAP, reason="This tests requries 'shap' package to run."
)
requires_no_ml_extras = pytest.mark.skipif(
    HAS_SKLEARN or HAS_XGBOOST,
    reason="This test requires 'scikit-learn' and 'xgboost' to not be installed.",
)

requires_lightgbm = pytest.mark.skipif(
    not HAS_LIGHTGBM, reason="This test requires 'lightgbm' package to run"
)


def skip_if_multiclass_classifition():
    if ES_VERSION < (7, 7):
        raise pytest.skip(
            "Skipped because multiclass classification "
            "isn't supported on Elasticsearch 7.6"
        )


def random_rows(data, size):
    return data[np.random.randint(data.shape[0], size=size), :]


def check_prediction_equality(es_model: MLModel, py_model, test_data):
    # Get some test results
    test_results = py_model.predict(np.asarray(test_data))
    es_results = es_model.predict(test_data)
    np.testing.assert_almost_equal(test_results, es_results, decimal=2)


def yield_model_id(analysis, analyzed_fields):
    import random
    import string
    import time

    suffix = "".join(random.choices(string.ascii_lowercase, k=4))
    job_id = "test-flights-regression-" + suffix
    dest = job_id + "-dest"

    response = ES_TEST_CLIENT.ml.put_data_frame_analytics(
        id=job_id,
        analysis=analysis,
        dest={"index": dest},
        source={"index": [FLIGHTS_SMALL_INDEX_NAME]},
        analyzed_fields=analyzed_fields,
    )
    assert response.meta.status == 200
    response = ES_TEST_CLIENT.ml.start_data_frame_analytics(id=job_id)
    assert response.meta.status == 200

    time.sleep(2)
    response = ES_TEST_CLIENT.ml.get_trained_models(model_id=job_id + "*")
    assert response.meta.status == 200
    assert response.body["count"] == 1
    model_id = response.body["trained_model_configs"][0]["model_id"]

    yield model_id

    ES_TEST_CLIENT.ml.delete_data_frame_analytics(id=job_id)
    ES_TEST_CLIENT.indices.delete(index=dest)
    ES_TEST_CLIENT.ml.delete_trained_model(model_id=model_id)


@pytest.fixture(params=[[0, 4], [0, 1], range(5)])
def regression_model_id(request):
    analysis = {
        "regression": {
            "dependent_variable": "FlightDelayMin",
            "max_trees": 3,
            "num_top_feature_importance_values": 0,
            "max_optimization_rounds_per_hyperparameter": 1,
            "prediction_field_name": "FlightDelayMin_prediction",
            "training_percent": 30,
            "randomize_seed": 1000,
            "loss_function": "mse",
            "early_stopping_enabled": True,
        }
    }
    all_includes = [
        "FlightDelayMin",
        "FlightDelayType",
        "FlightTimeMin",
        "DistanceMiles",
        "OriginAirportID",
    ]
    includes = [all_includes[i] for i in request.param]
    analyzed_fields = {
        "includes": includes,
        "excludes": [],
    }
    yield from yield_model_id(analysis=analysis, analyzed_fields=analyzed_fields)


@pytest.fixture(params=[[0, 6], [5, 6], range(7)])
def classification_model_id(request):
    analysis = {
        "classification": {
            "dependent_variable": "Cancelled",
            "max_trees": 5,
            "num_top_feature_importance_values": 0,
            "max_optimization_rounds_per_hyperparameter": 1,
            "prediction_field_name": "Cancelled_prediction",
            "training_percent": 50,
            "randomize_seed": 1000,
            "num_top_classes": -1,
            "class_assignment_objective": "maximize_accuracy",
            "early_stopping_enabled": True,
        }
    }
    all_includes = [
        "OriginWeather",
        "OriginAirportID",
        "DestCityName",
        "DestWeather",
        "DestRegion",
        "AvgTicketPrice",
        "Cancelled",
    ]
    includes = [all_includes[i] for i in request.param]
    analyzed_fields = {
        "includes": includes,
        "excludes": [],
    }
    yield from yield_model_id(analysis=analysis, analyzed_fields=analyzed_fields)


class TestMLModel:
    @requires_no_ml_extras
    def test_import_ml_model_when_dependencies_are_not_available(self):
        from eland.ml import MLModel  # noqa: F401

    @requires_sklearn
    def test_unpack_and_raise_errors_in_ingest_simulate(self, mocker):
        # Train model
        training_data = datasets.make_classification(n_features=5)
        classifier = DecisionTreeClassifier()
        classifier.fit(training_data[0], training_data[1])

        # Serialise the models to Elasticsearch
        feature_names = ["f0", "f1", "f2", "f3", "f4"]
        model_id = "test_decision_tree_classifier"
        test_data = [[0.1, 0.2, 0.3, -0.5, 1.0], [1.6, 2.1, -10, 50, -1.0]]

        es_model = MLModel.import_model(
            ES_TEST_CLIENT,
            model_id,
            classifier,
            feature_names,
            es_if_exists="replace",
            es_compress_model_definition=True,
        )

        # Mock the ingest.simulate API to return an error within {'docs': [...]}
        mock = mocker.patch.object(ES_TEST_CLIENT.ingest, "simulate")
        mock.return_value = {
            "docs": [
                {
                    "error": {
                        "type": "x_content_parse_exception",
                        "reason": "[1:1052] [inference_model_definition] failed to parse field [trained_model]",
                    }
                }
            ]
        }

        with pytest.raises(RuntimeError) as err:
            es_model.predict(test_data)

        assert repr(err.value) == (
            'RuntimeError("Failed to run prediction for model ID '
            "'test_decision_tree_classifier'\", {'type': 'x_content_parse_exception', "
            "'reason': '[1:1052] [inference_model_definition] failed to parse "
            "field [trained_model]'})"
        )

    @requires_sklearn
    @pytest.mark.parametrize("compress_model_definition", [True, False])
    @pytest.mark.parametrize("multi_class", [True, False])
    def test_decision_tree_classifier(self, compress_model_definition, multi_class):
        # Train model
        training_data = (
            datasets.make_classification(
                n_features=7,
                n_classes=3,
                n_clusters_per_class=2,
                n_informative=6,
                n_redundant=1,
            )
            if multi_class
            else datasets.make_classification(n_features=7)
        )
        classifier = DecisionTreeClassifier()
        classifier.fit(training_data[0], training_data[1])

        # Serialise the models to Elasticsearch
        feature_names = ["f0", "f1", "f2", "f3", "f4", "f5", "f6"]
        model_id = "test_decision_tree_classifier"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT,
            model_id,
            classifier,
            feature_names,
            es_if_exists="replace",
            es_compress_model_definition=compress_model_definition,
        )

        # Get some test results
        check_prediction_equality(
            es_model, classifier, random_rows(training_data[0], 20)
        )

        # Clean up
        es_model.delete_model()

    @requires_sklearn
    @pytest.mark.parametrize("compress_model_definition", [True, False])
    def test_decision_tree_regressor(self, compress_model_definition):
        # Train model
        training_data = datasets.make_regression(n_features=5)
        regressor = DecisionTreeRegressor()
        regressor.fit(training_data[0], training_data[1])

        # Serialise the models to Elasticsearch
        feature_names = ["f0", "f1", "f2", "f3", "f4"]
        model_id = "test_decision_tree_regressor"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT,
            model_id,
            regressor,
            feature_names,
            es_if_exists="replace",
            es_compress_model_definition=compress_model_definition,
        )
        # Get some test results
        check_prediction_equality(
            es_model, regressor, random_rows(training_data[0], 20)
        )

        # Clean up
        es_model.delete_model()

    @requires_sklearn
    @pytest.mark.parametrize("compress_model_definition", [True, False])
    def test_random_forest_classifier(self, compress_model_definition):
        # Train model
        training_data = datasets.make_classification(n_features=5)
        classifier = RandomForestClassifier()
        classifier.fit(training_data[0], training_data[1])

        # Serialise the models to Elasticsearch
        feature_names = ["f0", "f1", "f2", "f3", "f4"]
        model_id = "test_random_forest_classifier"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT,
            model_id,
            classifier,
            feature_names,
            es_if_exists="replace",
            es_compress_model_definition=compress_model_definition,
        )
        # Get some test results
        check_prediction_equality(
            es_model, classifier, random_rows(training_data[0], 20)
        )

        # Clean up
        es_model.delete_model()

    @requires_sklearn
    @pytest.mark.parametrize("compress_model_definition", [True, False])
    def test_random_forest_regressor(self, compress_model_definition):
        # Train model
        training_data = datasets.make_regression(n_features=5)
        regressor = RandomForestRegressor()
        regressor.fit(training_data[0], training_data[1])

        # Serialise the models to Elasticsearch
        feature_names = ["f0", "f1", "f2", "f3", "f4"]
        model_id = "test_random_forest_regressor"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT,
            model_id,
            regressor,
            feature_names,
            es_if_exists="replace",
            es_compress_model_definition=compress_model_definition,
        )
        # Get some test results
        check_prediction_equality(
            es_model, regressor, random_rows(training_data[0], 20)
        )

        match = f"Trained machine learning model {model_id} already exists"
        with pytest.raises(ValueError, match=match):
            MLModel.import_model(
                ES_TEST_CLIENT,
                model_id,
                regressor,
                feature_names,
                es_if_exists="fail",
                es_compress_model_definition=compress_model_definition,
            )

        # Clean up
        es_model.delete_model()

    @requires_xgboost
    @pytest.mark.parametrize("compress_model_definition", [True, False])
    @pytest.mark.parametrize("multi_class", [True, False])
    def test_xgb_classifier(self, compress_model_definition, multi_class):
        # test both multiple and binary classification
        if multi_class:
            skip_if_multiclass_classifition()
            training_data = datasets.make_classification(
                n_features=5, n_classes=3, n_informative=3
            )
            classifier = XGBClassifier(
                booster="gbtree", objective="multi:softmax", use_label_encoder=False
            )
        else:
            training_data = datasets.make_classification(n_features=5)
            classifier = XGBClassifier(booster="gbtree", use_label_encoder=False)

        # Train model
        classifier.fit(training_data[0], training_data[1])

        # Serialise the models to Elasticsearch
        feature_names = ["f0", "f1", "f2", "f3", "f4"]
        model_id = "test_xgb_classifier"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT,
            model_id,
            classifier,
            feature_names,
            es_if_exists="replace",
            es_compress_model_definition=compress_model_definition,
        )
        # Get some test results
        check_prediction_equality(
            es_model, classifier, random_rows(training_data[0], 20)
        )

        # Clean up
        es_model.delete_model()

    @requires_xgboost
    @pytest.mark.parametrize(
        "objective", ["multi:softmax", "multi:softprob", "binary:logistic"]
    )
    @pytest.mark.parametrize("booster", ["gbtree", "dart"])
    def test_xgb_classifier_objectives_and_booster(self, objective, booster):
        # test both multiple and binary classification
        if objective.startswith("multi"):
            skip_if_multiclass_classifition()
            training_data = datasets.make_classification(
                n_features=5, n_classes=3, n_informative=3
            )
            classifier = XGBClassifier(
                booster=booster, objective=objective, use_label_encoder=False
            )
        else:
            training_data = datasets.make_classification(n_features=5)
            classifier = XGBClassifier(
                booster=booster, objective=objective, use_label_encoder=False
            )

        # Train model
        classifier.fit(training_data[0], training_data[1])

        # Serialise the models to Elasticsearch
        feature_names = ["feature0", "feature1", "feature2", "feature3", "feature4"]
        model_id = "test_xgb_classifier"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT, model_id, classifier, feature_names, es_if_exists="replace"
        )
        # Get some test results
        check_prediction_equality(
            es_model, classifier, random_rows(training_data[0], 20)
        )

        # Clean up
        es_model.delete_model()

    @requires_xgboost
    @pytest.mark.parametrize("compress_model_definition", [True, False])
    @pytest.mark.parametrize(
        "objective",
        [
            "reg:squarederror",
            "reg:squaredlogerror",
            "reg:linear",
            "reg:logistic",
            "reg:pseudohubererror",
        ],
    )
    @pytest.mark.parametrize("booster", ["gbtree", "dart"])
    def test_xgb_regressor(self, compress_model_definition, objective, booster):
        # Train model
        training_data = datasets.make_regression(n_features=5)
        regressor = XGBRegressor(objective=objective, booster=booster)
        regressor.fit(
            training_data[0],
            np.exp(training_data[1] - np.max(training_data[1]))
            / sum(np.exp(training_data[1])),
        )

        # Serialise the models to Elasticsearch
        feature_names = ["f0", "f1", "f2", "f3", "f4"]
        model_id = "test_xgb_regressor"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT,
            model_id,
            regressor,
            feature_names,
            es_if_exists="replace",
            es_compress_model_definition=compress_model_definition,
        )
        # Get some test results
        check_prediction_equality(
            es_model, regressor, random_rows(training_data[0], 20)
        )

        # Clean up
        es_model.delete_model()

    @requires_xgboost
    def test_predict_single_feature_vector(self):
        # Train model
        training_data = datasets.make_regression(n_features=1)
        regressor = XGBRegressor()
        regressor.fit(training_data[0], training_data[1])

        # Get some test results
        test_data = [[0.1]]
        test_results = regressor.predict(np.asarray(test_data))

        # Serialise the models to Elasticsearch
        feature_names = ["f0"]
        model_id = "test_xgb_regressor"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT, model_id, regressor, feature_names, es_if_exists="replace"
        )

        # Single feature
        es_results = es_model.predict(test_data[0])

        np.testing.assert_almost_equal(test_results, es_results, decimal=2)

        # Clean up
        es_model.delete_model()

    @requires_lightgbm
    @pytest.mark.parametrize("compress_model_definition", [True, False])
    @pytest.mark.parametrize(
        "objective",
        ["regression", "regression_l1", "huber", "fair", "quantile", "mape"],
    )
    @pytest.mark.parametrize("booster", ["gbdt", "rf", "dart", "goss"])
    def test_lgbm_regressor(self, compress_model_definition, objective, booster):
        # Train model
        training_data = datasets.make_regression(n_features=5)
        if booster == "rf":
            regressor = LGBMRegressor(
                boosting_type=booster,
                objective=objective,
                bagging_fraction=0.5,
                bagging_freq=3,
            )
        else:
            regressor = LGBMRegressor(boosting_type=booster, objective=objective)
        regressor.fit(training_data[0], training_data[1])

        # Serialise the models to Elasticsearch
        feature_names = ["Column_0", "Column_1", "Column_2", "Column_3", "Column_4"]
        model_id = "test_lgbm_regressor"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT,
            model_id,
            regressor,
            feature_names,
            es_if_exists="replace",
            es_compress_model_definition=compress_model_definition,
        )
        # Get some test results
        check_prediction_equality(
            es_model, regressor, random_rows(training_data[0], 20)
        )

        # Clean up
        es_model.delete_model()

    @requires_lightgbm
    @pytest.mark.parametrize("compress_model_definition", [True, False])
    @pytest.mark.parametrize("objective", ["binary", "multiclass", "multiclassova"])
    @pytest.mark.parametrize("booster", ["gbdt", "dart", "goss"])
    def test_lgbm_classifier_objectives_and_booster(
        self, compress_model_definition, objective, booster
    ):
        # test both multiple and binary classification
        if objective.startswith("multi"):
            skip_if_multiclass_classifition()
            training_data = datasets.make_classification(
                n_features=5, n_classes=3, n_informative=3
            )
            classifier = LGBMClassifier(boosting_type=booster, objective=objective)
        else:
            training_data = datasets.make_classification(n_features=5)
            classifier = LGBMClassifier(boosting_type=booster, objective=objective)

        # Train model
        classifier.fit(training_data[0], training_data[1])

        # Serialise the models to Elasticsearch
        feature_names = ["Column_0", "Column_1", "Column_2", "Column_3", "Column_4"]
        model_id = "test_lgbm_classifier"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT,
            model_id,
            classifier,
            feature_names,
            es_if_exists="replace",
            es_compress_model_definition=compress_model_definition,
        )

        check_prediction_equality(
            es_model, classifier, random_rows(training_data[0], 20)
        )

        # Clean up
        es_model.delete_model()

    @requires_sklearn
    @requires_shap
    def test_export_regressor(self, regression_model_id):
        ed_flights = ed.DataFrame(ES_TEST_CLIENT, FLIGHTS_SMALL_INDEX_NAME).head(10)
        types = dict(ed_flights.dtypes)
        X = ed_flights.to_pandas().astype(types)

        model = MLModel(es_client=ES_TEST_CLIENT, model_id=regression_model_id)
        pipeline = model.export_model()
        pipeline.fit(X)

        predictions_sklearn = pipeline.predict(
            X, feature_names_in=pipeline["preprocessor"].get_feature_names_out()
        )
        response = ES_TEST_CLIENT.ml.infer_trained_model(
            model_id=regression_model_id,
            docs=X[pipeline["es_model"].input_field_names].to_dict("records"),
        )
        predictions_es = np.array(
            list(
                map(
                    itemgetter("FlightDelayMin_prediction"),
                    response.body["inference_results"],
                )
            )
        )
        np.testing.assert_array_almost_equal(predictions_sklearn, predictions_es)

        import pandas as pd

        X_transformed = pipeline["preprocessor"].transform(X=X)
        X_transformed = pd.DataFrame(
            X_transformed, columns=pipeline["preprocessor"].get_feature_names_out()
        )
        explainer = shap.TreeExplainer(pipeline["es_model"])
        shap_values = explainer.shap_values(
            X_transformed[pipeline["es_model"].feature_names_in_]
        )
        np.testing.assert_array_almost_equal(
            predictions_sklearn, shap_values.sum(axis=1) + explainer.expected_value
        )

    @requires_sklearn
    def test_export_classification(self, classification_model_id):
        ed_flights = ed.DataFrame(ES_TEST_CLIENT, FLIGHTS_SMALL_INDEX_NAME).head(10)
        X = ed.eland_to_pandas(ed_flights)

        model = MLModel(es_client=ES_TEST_CLIENT, model_id=classification_model_id)
        pipeline = model.export_model()
        pipeline.fit(X)

        predictions_sklearn = pipeline.predict(
            X, feature_names_in=pipeline["preprocessor"].get_feature_names_out()
        )
        prediction_proba_sklearn = pipeline.predict_proba(
            X, feature_names_in=pipeline["preprocessor"].get_feature_names_out()
        ).max(axis=1)

        response = ES_TEST_CLIENT.ml.infer_trained_model(
            model_id=classification_model_id,
            docs=X[pipeline["es_model"].input_field_names].to_dict("records"),
        )
        predictions_es = np.array(
            list(
                map(
                    lambda x: str(int(x["Cancelled_prediction"])),
                    response.body["inference_results"],
                )
            )
        )
        prediction_proba_es = np.array(
            list(
                map(
                    itemgetter("prediction_probability"),
                    response.body["inference_results"],
                )
            )
        )
        np.testing.assert_array_almost_equal(
            prediction_proba_sklearn, prediction_proba_es
        )
        np.testing.assert_array_equal(predictions_sklearn, predictions_es)

        import pandas as pd

        X_transformed = pipeline["preprocessor"].transform(X=X)
        X_transformed = pd.DataFrame(
            X_transformed, columns=pipeline["preprocessor"].get_feature_names_out()
        )
        explainer = shap.TreeExplainer(pipeline["es_model"])
        shap_values = explainer.shap_values(
            X_transformed[pipeline["es_model"].feature_names_in_]
        )
        log_odds = shap_values.sum(axis=1) + explainer.expected_value
        prediction_proba_shap = 1 / (1 + np.exp(-log_odds))
        # use probability of the predicted class
        prediction_proba_shap[prediction_proba_shap < 0.5] = (
            1 - prediction_proba_shap[prediction_proba_shap < 0.5]
        )
        np.testing.assert_array_almost_equal(
            prediction_proba_sklearn, prediction_proba_shap
        )

    @requires_xgboost
    @requires_sklearn
    @pytest.mark.parametrize("objective", ["binary:logistic", "reg:squarederror"])
    def test_xgb_import_export(self, objective):
        booster = "gbtree"

        if objective.startswith("binary:"):
            training_data = datasets.make_classification(n_features=5)
            xgb_model = XGBClassifier(
                booster=booster, objective=objective, use_label_encoder=False
            )
        else:
            training_data = datasets.make_regression(n_features=5)
            xgb_model = XGBRegressor(
                booster=booster, objective=objective, use_label_encoder=False
            )

        # Train model
        xgb_model.fit(training_data[0], training_data[1])

        # Serialise the models to Elasticsearch
        feature_names = ["feature0", "feature1", "feature2", "feature3", "feature4"]
        model_id = "test_xgb_model"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT, model_id, xgb_model, feature_names, es_if_exists="replace"
        )

        # Export suppose to fail
        with pytest.raises(ValueError) as ex:
            es_model.export_model()
        assert ex.match("Error initializing sklearn classifier.")

        # Clean up
        es_model.delete_model()

    @requires_lightgbm
    @pytest.mark.parametrize("objective", ["regression", "binary"])
    def test_lgbm_import_export(self, objective):
        booster = "gbdt"
        if objective == "binary":
            training_data = datasets.make_classification(n_features=5)
            lgbm_model = LGBMClassifier(boosting_type=booster, objective=objective)
        else:
            training_data = datasets.make_regression(n_features=5)
            lgbm_model = LGBMRegressor(boosting_type=booster, objective=objective)

        # Train model
        lgbm_model.fit(training_data[0], training_data[1])

        # Serialise the models to Elasticsearch
        feature_names = ["feature0", "feature1", "feature2", "feature3", "feature4"]
        model_id = "test_lgbm_model"

        es_model = MLModel.import_model(
            ES_TEST_CLIENT, model_id, lgbm_model, feature_names, es_if_exists="replace"
        )

        # Export suppose to fail
        with pytest.raises(ValueError) as ex:
            es_model.export_model()
        assert ex.match("Error initializing sklearn classifier.")

        # Clean up
        es_model.delete_model()
