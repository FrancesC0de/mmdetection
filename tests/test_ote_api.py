import functools
import numpy as np
import os.path as osp
import pytest
import random
import time
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor

from flaky import flaky
from sc_sdk.entities.annotation import Annotation, AnnotationScene, AnnotationSceneKind
from sc_sdk.entities.dataset_item import DatasetItem
from sc_sdk.entities.datasets import Dataset, Subset
from sc_sdk.entities.image import Image
from sc_sdk.entities.media_identifier import ImageIdentifier
from sc_sdk.entities.model import NullModel
from sc_sdk.entities.optimized_model import OptimizedModel
from sc_sdk.entities.resultset import ResultSet
from sc_sdk.entities.shapes.box import Box
from sc_sdk.entities.shapes.ellipse import Ellipse
from sc_sdk.entities.shapes.polygon import Polygon
from sc_sdk.entities.task_environment import TaskEnvironment
from sc_sdk.tests.test_helpers import generate_random_annotated_image, rerun_on_flaky_assert
from sc_sdk.usecases.tasks.interfaces.model_optimizer import IModelOptimizer
from sc_sdk.utils.project_factory import ProjectFactory

from mmdet.apis.ote.apis.detection import MMObjectDetectionTask, MMDetectionParameters, configurable_parameters

from e2e_test_system import select_configurable_parameters
from e2e_test_system import CollsysManager
from e2e_test_system import e2e_pytest


class TestOTEAPI(unittest.TestCase):
    """
    Collection of tests for OTE API and OTE Model Templates
    """

    def init_environment(self, configurable_parameters, number_of_images=500):
        project = ProjectFactory.create_project_single_task(name='OTEDetectionTestProject',
                                                            description='OTEDetectionTestProject',
                                                            label_names=['rectangle', 'ellipse', 'triangle'],
                                                            task_name='OTEDetectionTestTask',
                                                            configurable_parameters=configurable_parameters)
        self.addCleanup(lambda: ProjectFactory.delete_project_with_id(project.id))
        labels = project.get_labels()

        warnings.filterwarnings('ignore', message='.* coordinates .* are out of bounds.*')
        items = []
        for i in range(0, number_of_images):
            image_numpy, shapes = generate_random_annotated_image(image_width=640,
                                                                  image_height=480,
                                                                  labels=labels,
                                                                  max_shapes=20,
                                                                  min_size=50,
                                                                  max_size=100,
                                                                  random_seed=None)
            # Convert all shapes to bounding boxes
            box_shapes = []
            for shape in shapes:
                shape_labels = shape.get_labels(include_empty=True)
                shape = shape.shape
                if isinstance(shape, (Box, Ellipse)):
                    box = np.array([shape.x1, shape.y1, shape.x2, shape.y2], dtype=float)
                elif isinstance(shape, Polygon):
                    box = np.array([shape.min_x, shape.min_y, shape.max_x, shape.max_y], dtype=float)
                box = box.clip(0, 1)
                box_shapes.append(Annotation(Box(x1=box[0], y1=box[1], x2=box[2], y2=box[3]),
                                             labels=shape_labels))

            image = Image(name=f'image_{i}', project=project, numpy=image_numpy)
            image_identifier = ImageIdentifier(image.id)
            annotation = AnnotationScene(
                kind=AnnotationSceneKind.ANNOTATION,
                media_identifier=image_identifier,
                annotations=box_shapes)
            items.append(DatasetItem(media=image, annotation_scene=annotation))
        warnings.resetwarnings()

        rng = random.Random()
        rng.shuffle(items)
        for i, _ in enumerate(items):
            subset_region = i / number_of_images
            if subset_region >= 0.8:
                subset = Subset.TESTING
            elif subset_region >= 0.6:
                subset = Subset.VALIDATION
            else:
                subset = Subset.TRAINING
            items[i].subset = subset

        dataset = Dataset(items)
        task_node = project.tasks[-1]
        environment = TaskEnvironment(project=project, task_node=task_node)
        return project, environment, dataset

    @staticmethod
    def setup_configurable_parameters(template_dir, num_epochs=10):
        configurable_parameters = MMDetectionParameters()
        configurable_parameters.algo_backend.template.value = osp.join(template_dir, 'template.yaml')
        configurable_parameters.algo_backend.model.value = 'model.py'
        configurable_parameters.algo_backend.model_name.value = 'some_detection_model'
        configurable_parameters.learning_parameters.num_epochs.value = num_epochs
        return configurable_parameters

    @e2e_pytest
    @flaky(max_runs=2, rerun_filter=rerun_on_flaky_assert())
    def test_cancel_training_detection(self):
        """
        Tests starting and cancelling training.

        Flow of the test:
        - Creates a randomly annotated project with a small dataset containing 3 classes:
            ['rectangle', 'triangle', 'circle'].
        - Start training and give cancel training signal after 10 seconds. Assert that training
            stops within 35 seconds after that
        - Start training and give cancel signal immediately. Assert that training stops within 25 seconds.

        This test should be finished in under one minute on a workstation.
        """
        template_dir = osp.join('configs', 'ote', 'custom-object-detection', 'mobilenet_v2-2s_ssd-256x256')
        configurable_parameters = self.setup_configurable_parameters(template_dir, num_epochs=100)
        _, detection_environment, dataset = self.init_environment(configurable_parameters, 250)
        detection_task = MMObjectDetectionTask(task_environment=detection_environment)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='train_thread')

        json_configurable_parameters = configurable_parameters.to_json()
        setup = select_configurable_parameters(json_configurable_parameters)        
        setup["scenario"] = "api_cancel_training_detection"
        setup["subject"] = "custom-object-detection"
        setup["model"] = "mobilenet_v2-2s_ssd-256x256"
        setup["project"] = "ote"

        collsys_mgr = CollsysManager("main", setup)
        with collsys_mgr:
            collsys_mgr.log_internal_metric("checkpoint", "0", flush=True)

            # Test stopping after some time
            start_time = time.time()
            train_future = executor.submit(detection_task.train, dataset)
            time.sleep(10)  # give train_thread some time to initialize the model
            detection_task.cancel_training()

            duration_1 = time.time() - start_time
            threshhold_1 = 35
            collsys_mgr.log_final_metric("duration_1", duration_1)
            collsys_mgr.log_final_metric("threshhold_1", threshhold_1)
            collsys_mgr.log_internal_metric("checkpoint", "1", flush=True)
            
            # stopping process has to happen in less than 35 seconds
            info_1 = f"Expected to stop within {threshhold_1} seconds [flaky]."
            self.assertLess(duration_1, threshhold_1, info_1)
            train_future.result()

            # Test stopping immediately
            start_time = time.time()
            train_future = executor.submit(detection_task.train, dataset)
            time.sleep(1.0)
            detection_task.cancel_training()

            duration_2 = time.time() - start_time
            threshhold_2 = 25
            collsys_mgr.log_final_metric("duration_2", duration_2)
            collsys_mgr.log_final_metric("threshhold_2", threshhold_2)
            collsys_mgr.log_internal_metric("checkpoint", "2", flush=True)
            
            info_2 = f"Expected to stop within {threshhold_2} seconds [flaky]."
            self.assertLess(duration_2, threshhold_2, info_2)
            train_future.result()
            collsys_mgr.log_internal_metric("checkpoint", "3", flush=True)

    @staticmethod
    def eval(task, environment, dataset):
        start_time = time.time()
        result_dataset = task.analyse(dataset.with_empty_annotations())
        end_time = time.time()
        print(f'{len(dataset)} analysed in {end_time - start_time} seconds')
        result_set = ResultSet(
            model=environment.model,
            ground_truth_dataset=dataset,
            prediction_dataset=result_dataset
        )
        performance = task.compute_performance(result_set)
        return performance

    @flaky(max_runs=2, rerun_filter=rerun_on_flaky_assert())
    def train_and_eval(self, template_dir):
        """
        Run training, analysis, evaluation and model optimization

        Flow of the test:
        - Creates a randomly annotated project with a small dataset containing 3 classes:
            ['rectangle', 'triangle', 'circle'].
        - Trains a model for 10 epochs. Asserts that the returned model is not a NullModel, that
            validation F-measure is larger than the threshold and also that OpenVINO optimization runs successfully.
        - Reloads the model in the task and recompute the performance. Asserts that the performance
            difference between the original and the reloaded model is smaller than 1e-4. Ideally there should be no
            difference at all.
        """
        configurable_parameters = self.setup_configurable_parameters(template_dir, num_epochs=5)
        _, detection_environment, dataset = self.init_environment(configurable_parameters, 250)
        task = MMObjectDetectionTask(task_environment=detection_environment)
        self.addCleanup(task._delete_scratch_space)

        print('Task initialized, model training starts.')
        # Train the task.
        # train_task checks that the returned model is not a NullModel, that the task returns an OptimizedModel and that
        # validation f-measure is higher than the threshold, which is a pretty low bar
        # considering that the dataset is so easy

        model = task.train(dataset=dataset)
        self.assertFalse(isinstance(model, NullModel))

        if isinstance(task, IModelOptimizer):
            optimized_models = task.optimize_loaded_model()
            self.assertGreater(len(optimized_models), 0, 'Task must return an Optimised model.')
            for m in optimized_models:
                info = "Optimised model must be an Openvino or DeployableTensorRT model."
                self.assertIsInstance(m, OptimizedModel, info)

        # Run inference
        validation_performance = self.eval(task, detection_environment, dataset)
        print(f'Evaluated model to have a performance of {validation_performance}')
        score_threshold = 0.5
        self.assertGreater(validation_performance.score.value, score_threshold,
            f'Expected F-measure to be higher than {score_threshold} [flaky]')

        print('Reloading model.')
        # Re-load the model
        task.load_model(task.task_environment)

        print('Reevaluating model.')
        # Performance should be the same after reloading
        performance_after_reloading = self.eval(task, detection_environment, dataset)
        performance_delta = performance_after_reloading.score.value - validation_performance.score.value
        perf_delta_tolerance = 0.0001

        self.assertLess(np.abs(performance_delta), perf_delta_tolerance,
                        msg=f'Expected no or very small performance difference after reloading. Performance delta '
                            f'({validation_performance.score.value} vs {performance_after_reloading.score.value}) was '
                            f'larger than the tolerance of {perf_delta_tolerance}')

        print(f'Performance: {validation_performance.score.value:.4f}')
        print(f'Performance after reloading: {performance_after_reloading.score.value:.4f}')
        print(f'Performance delta after reloading: {performance_delta:.6f}')

        json_configurable_parameters = configurable_parameters.to_json()
        return select_configurable_parameters(json_configurable_parameters), {
            "score_threshold": score_threshold,
            "score_tolerance": perf_delta_tolerance,
            "score_before_reload": validation_performance.score.value,
            "score_after_reload": performance_after_reloading.score.value
        }
        
    @e2e_pytest
    @flaky(max_runs=2, rerun_filter=rerun_on_flaky_assert())
    def test_training_custom_mobilenetssd_256(self):
        setup = {
            "project": "ote",
            "scenario": "api_training",
            "subject": "custom-object-detection",
            "model": "mobilenet_v2-2s_ssd-256x256"
        }
        collsys_mgr = CollsysManager("main", setup)
        with collsys_mgr:
            params, results = self.train_and_eval(osp.join('configs', 'ote', setup['subject'], setup['model']))
            for key, value in params.items(): collsys_mgr.update_metadata(key, value)
            for key, value in results.items(): collsys_mgr.log_final_metric(key, value)

    @e2e_pytest
    @flaky(max_runs=2, rerun_filter=rerun_on_flaky_assert())
    def test_training_custom_mobilenetssd_384(self):
        setup = {
            "project": "ote",
            "scenario": "api_training",
            "subject": "custom-object-detection",
            "model": "mobilenet_v2-2s_ssd-384x384"
        }
        collsys_mgr = CollsysManager("main", setup)
        with collsys_mgr:
            params, results = self.train_and_eval(osp.join('configs', 'ote', setup['subject'], setup['model']))
            for key, value in params.items(): collsys_mgr.update_metadata(key, value)
            for key, value in results.items(): collsys_mgr.log_final_metric(key, value)

    @e2e_pytest
    @flaky(max_runs=2, rerun_filter=rerun_on_flaky_assert())
    def test_training_custom_mobilenetssd_512(self):
        setup = {
            "project": "ote",
            "scenario": "api_training",
            "subject": "custom-object-detection",
            "model": "mobilenet_v2-2s_ssd-512x512"
        }
        collsys_mgr = CollsysManager("main", setup)
        with collsys_mgr:
            params, results = self.train_and_eval(osp.join('configs', 'ote', setup['subject'], setup['model']))
            for key, value in params.items(): collsys_mgr.update_metadata(key, value)
            for key, value in results.items(): collsys_mgr.log_final_metric(key, value)
