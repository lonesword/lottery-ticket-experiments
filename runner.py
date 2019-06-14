import time
import torch
import torchvision
import numpy as np

from torch import nn
from torchvision.transforms import transforms

import hyperparameter_presets

# A helper class that takes a model and dataset, and runs the experiment on it.
from networks import FullyConnectedMNIST
from utils import get_zero_count, apply_mask_dict_to_weight_dict


class ExperimentRunner:
    TRAINING_DURATION_SECONDS = "training_duration_seconds"
    FINAL_VALIDATION_ACCURACY = "final_validation_accuracy"
    TEST_ACCURACY = "test_accuracy"
    DEVICE = "device"
    ZERO_COUNTS_IN_WEIGHTS = "zero_counts_in_weights"

    def __init__(self, model, num_epochs=10, batch_size=200, learning_rate=5e-3, learning_rate_decay=0.95):
        self.model = model
        self.learning_rate = learning_rate
        self.reg = 0.001  # Should this be a hyper parameter?
        self.num_epochs = num_epochs
        self.batch_size = batch_size
        self.learning_rate_decay = learning_rate_decay
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.stats = {
            self.DEVICE: str(self.device)
        }
        self.update_stat(self.ZERO_COUNTS_IN_WEIGHTS, self.get_zero_count_in_weights())

    def print_stats(self):
        print(self.stats)

    def update_stat(self, stat_name, value):
        self.stats[stat_name] = value

    @staticmethod
    def update_lr(optimizer, lr):
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

    def train(self, input_size, train_dataloader, validation_dataloader):
        criterion = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.learning_rate, weight_decay=self.reg)

        training_start_time = time.time()
        for epoch in range(self.num_epochs):
            for i, (images, labels) in enumerate(train_dataloader):
                # Move tensors to the configured device
                images = images.to(self.device)
                labels = labels.to(self.device)
                images = images.view(self.batch_size, input_size)
                optimizer.zero_grad()
                output = self.model(images)
                loss = criterion(output, labels)
                loss.backward()
                optimizer.step()

            print('Epoch [{}/{}], Loss: {:.4f}'.format(epoch + 1, self.num_epochs, loss.item()))
            lr = self.learning_rate * self.learning_rate_decay
            self.update_lr(optimizer, lr)
            validation_accuracy = self.validate(input_size, validation_dataloader)

        self.update_stat(self.TRAINING_DURATION_SECONDS, time.time() - training_start_time)
        self.update_stat(self.FINAL_VALIDATION_ACCURACY, validation_accuracy)
        self.update_stat(self.ZERO_COUNTS_IN_WEIGHTS, self.get_zero_count_in_weights())

    def validate(self, input_size, validation_dataloader):
        with torch.no_grad():
            correct = 0
            total = 0
            for images, labels in validation_dataloader:
                images = images.to(self.device)
                labels = labels.to(self.device)

                images = images.view(self.batch_size, input_size)
                scores = self.model.forward(images)

                predicted = []

                def get_class(x):
                    return torch.argsort(x)[-1]

                for i in range(0, len(scores)):
                    predicted.append(get_class(scores[i]))

                predicted = torch.stack(predicted)
                total += labels.size(0)
                correct += (predicted == labels).sum().item()
            validation_accuracy = 100 * correct / total
            print('Validation accuracy is: {} %'.format(validation_accuracy))

        return validation_accuracy

    def test(self, input_size, test_dataloader):
        with torch.no_grad():
            correct = 0
            total = 0
            for images, labels in test_dataloader:
                images = images.to(self.device)
                labels = labels.to(self.device)

                images = images.view(self.batch_size, input_size)
                scores = self.model.forward(images)

                predicted = []

                def get_class(x):
                    return torch.argsort(x)[-1]

                for i in range(0, len(scores)):
                    predicted.append(get_class(scores[i]))

                predicted = torch.stack(predicted)
                total += labels.size(0)
                correct += (predicted == labels).sum().item()

            test_accuracy = 100 * correct / total
            print('Validation accuracy is: {} %'.format(test_accuracy))

        self.update_stat(self.TEST_ACCURACY, test_accuracy)
        return test_accuracy

    def get_initial_mask(self):
        mask_dict = dict()
        for name, parameter in self.model.named_parameters():
            if name.endswith('weight'):
                mask_dict[name] = torch.ones(parameter.data.shape).byte()

        return mask_dict

    def prune(self, mask_dict, prune_percent=0.1):
        # We assume that all layers are pruned by the same percentage
        # Yes, we prune per layer, not globally

        for name, parameter in self.model.named_parameters():
            # TODO: Check if we should indeed ignore the bias
            if name.endswith('weight'):
                current_mask = mask_dict.get(name, None)
                new_mask = self.get_new_mask(prune_percent, parameter.data, current_mask)
                mask_dict[name] = new_mask

        return mask_dict

    @staticmethod
    def get_new_mask(prune_percent, data, current_mask):
        # TODO: Remove the random .cuda() in the below line
        sorted_weights = torch.sort(torch.abs(torch.masked_select(data, current_mask.cuda()))).values
        cutoff_index = np.round(prune_percent * len(sorted_weights)).astype(int)
        cutoff = sorted_weights[cutoff_index]
        return torch.from_numpy(np.where(np.abs(data) <= cutoff, np.zeros(current_mask.shape), current_mask)).byte()

    def get_zero_count_in_weights(self):
        # In each linear layer in the network, count the number of zeros. Useful for debugging
        zeros_info_dict = dict()

        for name, param in self.model.named_parameters():
            if name.endswith('weight'):
                zeros_info_dict[name] = get_zero_count(param.data)

        return zeros_info_dict


def mnist_experiment():
    num_classes = hyperparameter_presets.FULLY_CONNECTED_MNIST['num_classes']
    input_size = hyperparameter_presets.FULLY_CONNECTED_MNIST['input_size']
    hidden_sizes = hyperparameter_presets.FULLY_CONNECTED_MNIST['hidden_sizes']

    # Temporary parameters. Should probably move this to the hyper parameters file as well
    num_training = 58000
    num_validation = 2000
    batch_size = 200

    # Prepare the dataset
    to_tensor_transform = transforms.Compose([transforms.ToTensor()])
    mnist_dataset = torchvision.datasets.MNIST(root='datasets/', train=True, download=True, transform=to_tensor_transform)
    mnist_test_dataset = torchvision.datasets.MNIST(root='datasets/', train=False, transform=to_tensor_transform)
    mask = list(range(num_training))
    mnist_train_dataset = torch.utils.data.Subset(mnist_dataset, mask)
    mask = list(range(num_training, num_training + num_validation))
    mnist_val_dataset = torch.utils.data.Subset(mnist_dataset, mask)

    # Load the dataset
    mnist_train_loader = torch.utils.data.DataLoader(dataset=mnist_train_dataset, batch_size=batch_size, shuffle=True)
    mnist_val_loader = torch.utils.data.DataLoader(dataset=mnist_val_dataset, batch_size=batch_size, shuffle=False)
    mnist_test_loader = torch.utils.data.DataLoader(dataset=mnist_test_dataset, batch_size=batch_size, shuffle=False)

    model = FullyConnectedMNIST(input_size, hidden_sizes, num_classes)
    model.cuda()
    # I now have a mask and pre_init
    # 1. Initialize another network with pre_init, and set a mask on it
    # 2. while training, make sure that the weights are indeed 0 according to the mask - set a breakpoint
    # after an epoch and verify this
    # 3. Repeat 1 & 2 one more time
    # 4. Check the test accuracy of the pruned network on the test data set.

    experiment = ExperimentRunner(model)
    mask_dict = experiment.get_initial_mask()
    experiment.train(input_size, mnist_train_loader, mnist_val_loader)
    experiment.test(input_size, mnist_test_loader)
    experiment.print_stats()
    mask_dict = experiment.prune(mask_dict, prune_percent=0.2)
    initial_weights_after_mask = apply_mask_dict_to_weight_dict(mask_dict, experiment.model.initial_weights)

    model_2 = FullyConnectedMNIST(input_size, hidden_sizes, num_classes, pre_init=initial_weights_after_mask, mask_dict=mask_dict)
    model_2.cuda()
    experiment = ExperimentRunner(model_2)
    experiment.train(input_size, mnist_train_loader, mnist_val_loader)
    experiment.test(input_size, mnist_test_loader)
    experiment.print_stats()

if __name__ == "__main__":
    mnist_experiment()

