import argparse
import copy
import h5py
from data_loader import DataLoader
from augmentation_3D import Augmentation_3D
import skimage.segmentation
import numpy as np
from tqdm import tqdm
from utils import plot_volume
import tensorflow as tf
from tensorflow.python.client import device_lib
from sklearn import metrics
from sklearn.linear_model import LinearRegression

gpus = tf.config.experimental.list_physical_devices("GPU")
tf.config.experimental.set_memory_growth(gpus[0], True)
device_name = tf.test.gpu_device_name()
print("Available Devices: {}".format(device_lib.list_local_devices()))
print("GPU name: {}".format(tf.test.gpu_device_name()))

parser = argparse.ArgumentParser()

# directories
parser.add_argument('--data_root', type=str, default='data/', help='path to the root of data directory')
parser.add_argument('--dataset', type=str, default='dataset_APEX.hdf5', help='dataset name')
parser.add_argument('--model_path', type=str, default='models/apex_model.h5', help='path to the model')
parser.add_argument('--weights_path', type=str, default='weights/_fold0_apex_weights.h5',
                    help='path to the model weights for testing')

parser.add_argument('--iterations', type=int, default=100,
                    help='number of times which we want to apply different random perturbations')
parser.add_argument('--transformations', type=list,
                    default=['rotate', 'flip_horizontally', 'flip_vertically', 'brightness'],
                    help='just some transformation supported')
parser.add_argument('--n_pert', type=int, default=10, help='number of random generated perturbations for each sample')
parser.add_argument('--steps', type=int, default=10, help='Steps which data generated to be saved')
args = parser.parse_args()

# create data loader
data_loader = DataLoader(hdf5_path=args.data_root + args.dataset)
dset_x, dset_y = data_loader.read_data()


class Lime():
    def __init__(self, volume):
        self.volume = volume

    def generate_segmentation(self, n_segments=25, compactness=0.3, max_iter=1000):
        '''
        generate 3 segmentation for images of a volume
        '''
        superpixels = []
        first_layer = self.volume[:, :, 1]  # layer is a 2D array now
        temp_volume = np.repeat(first_layer[None, :], 3, axis=0).transpose(1, 2,
                                                                           0)  # create a 3-layer tensor by repeating the layer
        first_layer_super_pixel = skimage.segmentation.slic(temp_volume,
                                                            n_segments=n_segments,
                                                            compactness=compactness,
                                                            max_iter=max_iter,
                                                            start_label=1)

        superpixels = tf.expand_dims(first_layer_super_pixel, axis=2)
        superpixels = np.repeat(superpixels, 3, axis=2)

        return superpixels

    def generate_perturbations(self, superpixels) -> list:
        assert superpixels.shape == self.volume.shape

        layers_perturbation = []
        first_layer_n_unique_values = len(np.unique(superpixels[:, :, 0]))
        first_layer_p = np.random.binomial(1, 0.5, size=(1, first_layer_n_unique_values)).squeeze()
        for i in range(superpixels.shape[-1]):  # over volume layers
            n_unique_values = len(np.unique(superpixels[:, :, i]))
            if n_unique_values == first_layer_n_unique_values:
                layers_perturbation.append(first_layer_p)
            elif n_unique_values < first_layer_n_unique_values:
                layers_perturbation.append(first_layer_p[:n_unique_values + 1])
            else:
                layers_perturbation.append(
                    np.pad(first_layer_p, (0, n_unique_values - first_layer_n_unique_values), 'constant'))
            # p = np.random.binomial(1, 0.5, size=(1, n_unique_values)).squeeze()
            # layers_perturbation.append(p)

        return layers_perturbation

    def apply_perturbations(self, layers_perturbation, superpixels):
        perturbed_volume = []
        mask_volume = []

        for i in range(len(layers_perturbation)):  # loop over the layers of a volume
            active_pixels = np.where(layers_perturbation[i] == 1)[0]
            mask = np.zeros(superpixels[:, :, i].shape)
            for active in active_pixels:
                mask[superpixels[:, :, i] == active] = 1

            perturbed_image = copy.deepcopy(self.volume[:, :, i])
            perturbed_image = perturbed_image * mask
            perturbed_volume.append(perturbed_image)
            mask_volume.append(mask)

        perturbed_volume = np.array(perturbed_volume).transpose(1, 2, 0)
        mask_volume = np.array(mask_volume).transpose(1, 2, 0)

        return perturbed_volume, mask_volume

    def extract_best_superpixels(self, perts, predictions, num_top_features=4):
        # num_superpixels = perts.shape[-1]
        best_superpixels = []
        layer_1 = []
        layer_2 = []
        layer_3 = []
        for p in perts:
            layer_1.append(p[0])
            layer_2.append(p[1])
            layer_3.append(p[2])

        layers = [layer_1, layer_2, layer_3]
        for l in layers:  # since there are 3 slices in each volume
            layer_perts = np.array(l)
            num_superpixels = layer_perts.shape[-1]
            # Compute distances between the original image and each of the perturbed
            # images and compute weights (importance) of each perturbed image
            original_image = np.ones(num_superpixels)[np.newaxis, :]  # Perturbation with all superpixels enabled
            distances = metrics.pairwise_distances(layer_perts, original_image, metric='cosine').ravel()
            # Use kernel function to compute weights
            kernel_width = 0.25
            weights = np.sqrt(np.exp(-(distances ** 2) / kernel_width ** 2))  # Kernel function
            # Use perturbations, predictions and weights to fit an explainable (linear) model
            lr = LinearRegression()
            lr.fit(X=layer_perts, y=predictions, sample_weight=weights)
            coeff = lr.coef_
            top_features = np.argsort(coeff)[-num_top_features:]
            best_superpixels.append(top_features)

        best_superpixels = np.array(best_superpixels)
        return best_superpixels


# load trained model for evaluating
print("Model loaded from: {}".format(args.model_path))
model = tf.keras.models.load_model(args.model_path)
print("Weights loaded from: {}".format(args.weights_path))
model.load_weights(args.weights_path)

# transform dataset
augmentation = Augmentation_3D(transformations=args.transformations)
data = tf.data.Dataset.from_tensor_slices((dset_x, dset_y))

print("Preparing datasets ...")
dataset = (
    data
        .map(augmentation.validation_preprocessing)
        .batch(1)  # since we want to have prediction for each sample
        .prefetch(2)
)
data_size = len(data)
# we don't need these anymore
del dset_x, dset_y, data

correct_predicted_samples_X = []
correct_predicted_samples_Y = []
best_perturbations_X = []
best_perturbations_Y = []
indices = []
best_mask_volumes = []
idx = 0
with tf.device(device_name=device_name):
    for sample in dataset:
        print("============================")
        print('Processing on sample:{}/{}'.format(idx, data_size))
        volume = sample[0][0, :, :, :, 0]  # volume shape must be (128, 128, 3)
        target = sample[1].numpy()[0, 0]
        if target == 1:
            temp_volume = tf.expand_dims(volume, axis=3)
            temp_volume = tf.expand_dims(temp_volume, axis=0)
            pred = model.predict(temp_volume)[0, 0]
            if (pred >= 0.5 and target == 0) or (pred < 0.5 and target == 1):  # wrong prediction and pass it
                print("IGNORED. The prediction was wrong.")
                idx += 1
                continue
            else:
                correct_predicted_samples_X.append(volume)
                correct_predicted_samples_Y.append(target)
            lime = Lime(volume)
            best_pred = 0.5
            best_volume = None  # generated volume by lime with the most accurate prediction
            best_idx = 0
            perts = []
            predictions = []
            superpixels = lime.generate_segmentation(max_iter=args.iterations, n_segments=25, compactness=0.5)
            for i in tqdm(range(args.n_pert)):
                layers_perturbation = lime.generate_perturbations(superpixels)
                perturbed_volume, _ = lime.apply_perturbations(layers_perturbation, superpixels)
                temp_volume = tf.expand_dims(perturbed_volume, axis=3)
                temp_volume = tf.expand_dims(temp_volume, axis=0)
                pred = model.predict(temp_volume)[0, 0]
                perts.append(layers_perturbation)
                predictions.append(pred)
                if target == 1:
                    if pred > best_pred:
                        best_pred = pred
                        best_volume = perturbed_volume
                        best_idx = i
                else:  # target = 0
                    if pred < best_pred:
                        best_pred = pred
                        best_volume = perturbed_volume
                        best_idx = i
            best_superpixels = lime.extract_best_superpixels(perts, predictions, num_top_features=1)
            mask = []

            for i in range(3):
                layer_mask = np.array([0] * len(perts[0][i]))
                layer_mask[best_superpixels[i]] = True  # Activate top superpixels
                mask.append(layer_mask)
            final_perturbed_volume, mask_volume = lime.apply_perturbations(mask, superpixels)
            # plot_volume(final_perturbed_volume, save_fig=True, filename='fig_'+str(idx))
            temp_volume = tf.expand_dims(final_perturbed_volume, axis=3)
            temp_volume = tf.expand_dims(temp_volume, axis=0)
            print("Best superpixel model prediction: {}".format(model.predict(temp_volume)[0, 0]))
            print(
                "The {}/{} perturbation with class {} is chosen with prediction score: {}".format(best_idx, args.n_pert,
                                                                                                  target, best_pred))

        # save the results
        if target == 1 and best_volume is not None:  # only save the samples with class 1
            best_perturbations_X.append(final_perturbed_volume)
            best_perturbations_Y.append(target)
            indices.append(idx)
            best_mask_volumes.append(mask_volume)
        else:
            print()
        idx += 1
        if idx % args.steps == 0:
            correct_predicted_samples_X_array = np.array(correct_predicted_samples_X)
            correct_predicted_samples_Y_array = np.array(correct_predicted_samples_Y)
            best_perturbations_X_array = np.array(best_perturbations_X)
            best_perturbations_Y_array = np.array(best_perturbations_Y)
            best_mask_volumes_array = np.array(best_mask_volumes)
            with h5py.File(args.data_root + 'correct_predictions_apex_1.hdf5', 'w') as hf:
                hf.create_dataset('idx', data=np.array(indices), shape=np.array(indices).shape,
                                  compression='gzip', chunks=True)
                hf.create_dataset('X', data=correct_predicted_samples_X_array,
                                  shape=correct_predicted_samples_X_array.shape,
                                  compression='gzip', chunks=True)
                hf.create_dataset('Y', data=correct_predicted_samples_Y_array,
                                  shape=(len(correct_predicted_samples_Y_array), 1),
                                  compression='gzip', chunks=True)
            print('{} correct predictions saved at: {}'.format(correct_predicted_samples_X_array.shape[0],
                                                               args.data_root + 'correct_predictions_apex_1.hdf5'))
            with h5py.File(args.data_root + 'perturbations_apex_1.hdf5', 'w') as hf:
                hf.create_dataset('idx', data=np.array(indices), shape=np.array(indices).shape,
                                  compression='gzip', chunks=True)
                hf.create_dataset('X', data=best_perturbations_X_array, shape=best_perturbations_X_array.shape,
                                  compression='gzip',
                                  chunks=True)
                hf.create_dataset('Y', data=best_perturbations_Y_array, shape=(len(best_perturbations_Y_array), 1),
                                  compression='gzip', chunks=True)
            print('best perturbations saved at: {}'.format(args.data_root + 'perturbations_apex_1.hdf5'))
            with h5py.File(args.data_root + 'masks_apex_1.hdf5', 'w') as hf:
                hf.create_dataset('idx', data=np.array(indices), shape=np.array(indices).shape,
                                  compression='gzip', chunks=True)
                hf.create_dataset('mask', data=best_mask_volumes_array, shape=best_mask_volumes_array.shape,
                                  compression='gzip', chunks=True)
                hf.create_dataset('Y', data=best_perturbations_Y_array, shape=(len(best_perturbations_Y_array), 1),
                                  compression='gzip', chunks=True)
            print('best masks saved at: {}'.format(args.data_root + 'masks_apex_1.hdf5'))

            del correct_predicted_samples_X_array
            del correct_predicted_samples_Y_array
            del best_perturbations_X_array
            del best_perturbations_Y_array
            del best_mask_volumes_array

correct_predicted_samples_X = np.array(correct_predicted_samples_X)
correct_predicted_samples_Y = np.array(correct_predicted_samples_Y)
best_perturbations_X = np.array(best_perturbations_X)
best_perturbations_Y = np.array(best_perturbations_Y)
best_mask_volumes_array = np.array(best_mask_volumes)

with h5py.File(args.data_root + 'correct_predictions_apex_1.hdf5', 'w') as hf:
    hf.create_dataset('idx', data=np.array(indices), shape=np.array(indices).shape, compression='gzip', chunks=True)
    hf.create_dataset('X', data=correct_predicted_samples_X, shape=correct_predicted_samples_X.shape,
                      compression='gzip', chunks=True)
    hf.create_dataset('Y', data=correct_predicted_samples_Y, shape=(len(correct_predicted_samples_Y), 1),
                      compression='gzip', chunks=True)

with h5py.File(args.data_root + 'perturbations_apex_1.hdf5', 'w') as hf:
    hf.create_dataset('idx', data=np.array(indices), shape=np.array(indices).shape, compression='gzip', chunks=True)
    hf.create_dataset('mask', data=np.array(best_mask_volumes), shape=np.array(best_mask_volumes).shape,
                      compression='gzip', chunks=True)
    hf.create_dataset('X', data=best_perturbations_X, shape=best_perturbations_X.shape, compression='gzip', chunks=True)
    hf.create_dataset('Y', data=best_perturbations_Y, shape=(len(best_perturbations_Y), 1), compression='gzip',
                      chunks=True)

with h5py.File(args.data_root + 'masks_apex_1.hdf5', 'w') as hf:
    hf.create_dataset('idx', data=np.array(indices), shape=np.array(indices).shape, compression='gzip', chunks=True)
    hf.create_dataset('mask', data=best_mask_volumes_array, shape=best_mask_volumes_array.shape, compression='gzip',
                      chunks=True)
    hf.create_dataset('Y', data=best_perturbations_Y, shape=(len(best_perturbations_Y), 1), compression='gzip',
                      chunks=True)