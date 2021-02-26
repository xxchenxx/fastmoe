r'''
The adaptor to seamlessly enable FastMoE in Megatron-LM v2.0 with at most two
lines of modification.
See `examples/megatron` for usage instructions.
'''
import torch.nn as nn

from .transformer import FMoETransformerMLP
from .distributed import DistributedGroupedDataParallel
from .utils import get_torch_default_comm


class _FakeMegatronMLP(nn.Module):
    r'''
    A fake mlp without model parallelism for correctness testing
    '''
    def __init__(self, args, group):
        super().__init__()
        self.fc1 = nn.Linear(args.hidden_size, args.hidden_hidden_size)
        self.fc2 = nn.Linear(args.hidden_hidden_size, args.hidden_size)
    def forward(self, x):
        x = self.fc1(x)
        x = F.gelu(x)
        x = self.fc2(x)
        return x, torch.zeros_like(x)


def _random_init_weight(self, rng):
    r'''
    Copied from torch.nn.init.kaiming_uniform_
    '''
    fan = nn.init._calculate_correct_fan(self.weight[0], 'fan_in')
    gain = nn.init.calculate_gain('leaky_relu', math.sqrt(5))
    std = gain / math.sqrt(fan)
    bound = math.sqrt(3.0) * std
    device = self.weight.device
    dtype = self.weight.dtype
    weight = rng.uniform(-bound, bound, size=tuple(self.weight.size()))
    self.weight.data = torch.tensor(weight, dtype=dtype, device=device)

    if self.bias is not None:
        fan_in, _ = nn.init._calculate_fan_in_and_fan_out(self.weight[0])
        bound = 1 / math.sqrt(fan_in)
        bias = rng.uniform(-bound, bound, size=tuple(self.bias.size()))
        self.bias.data = torch.tensor(bias, dtype=dtype, device=device)


class MegatronMLP(FMoETransformerMLP):
    r'''
    Make the FMoETransformerMLP layer that distributes experts across
    communication group `group` to replace the original MLP layer in Megatron.
    '''
    def __init__(self, args, group):
        assert (args.seq_length * args.micro_batch_size
                % args.tensor_model_parallel_size == 0
        ), "Batch size x sequence length should be multiple of mp size"
        if not args.distributed_experts:
            world_size = 1
        else:
            world_size = args.world_size
        super().__init__(args.num_experts,
                top_k=args.top_k,
                d_model=args.hidden_size, d_hidden=args.hidden_hidden_size,
                world_size=world_size, mp_group=group,
                expert_dp_comm='none' if args.distributed_experts else 'dp')
        self.hidden_size = args.hidden_size
        self.rank = args.rank
        self.reset_parameters()

    def reset_parameters(self):
        r'''
        Initialize the weight as linear layers.
        As megatron is using fixed random seed for some nasty stuff, an
        additional numpy rng is used.  
        '''
        rng = np.random.default_rng(np.random.randint(2048) + self.rank)
        _random_init_weight(self.experts.htoh4, rng)
        _random_init_weight(self.experts.h4toh, rng)

    def forward(self, inp):
        return super().forward(inp), torch.zeros(self.hidden_size,
                dtype=inp.dtype, device=inp.device)


def fmoefy(model, num_experts=None, distributed_experts=True,
        hidden_hidden_size=None, top_k=None):
    r'''
    Replace MLP layers in a transformer-based model in Megatron by MoE.
    * `model` should be a standard Megatron model that has
    `model.language_model.transformer.layers` as transformer layers, which is an
    array of transformer blocks that contain an `mlp` member.
    * `distributed_expert` is set to True if different experts are located in
    different workers. Otherwise, the experts on the workers are identical, and
    they are trained in data-parallel mode. This can be useful when testing on
    small models that do not require high training throughput or large parameter
    capacity.
    Note that pipeline parallel is not supported yet. When distributed experts
    are enabled, their communicator should be Megatron's
    tensor_model_parall_comm x data_parallel_comm, which is not created.
    '''
    from megatron import get_args
    from megatron import mpu
    args = get_args()
    if num_experts is not None:
        args.num_experts = num_experts
    assert (
        'num_experts' in args
    ), 'num_experts should be specified in arguments or fmoefy function'

    if hidden_hidden_size is not None:
        args.hidden_hidden_size = hidden_hidden_size
    elif not hasattr(args, 'hidden_hidden_size'):
        args.hidden_hidden_size = args.hidden_size * 4

    if top_k is not None:
        args.top_k = top_k
    elif not hasattr(args, 'top_k'):
        args.top_k = 2

    # Set distributed_experts to None to use default setting in args
    if distributed_experts is not None:
        args.distributed_experts = distributed_experts

    for l in model.language_model.transformer.layers:
        l.mlp = MegatronMLP(args, mpu.get_model_parallel_group())
    return model


class DistributedDataParallel(DistributedGroupedDataParallel):
    r'''
    A wrapper that is used to replace the DDP module provided by Megatron, which
    is adapted to enable the sophiscated parallel and reduction strategies in
    Fast MoE.
    '''
    def __init__(self, module):
        from megatron import mpu
        super().__init__(
            module,
            mp_group=mpu.get_model_parallel_group(),
            dp_group=mpu.get_data_parallel_group()
        )

    def state_dict(self, *args, **kwargs):
        r'''
        Keep consitency with Megatron
        '''
        return self.module.state_dict(*args, **kwargs)

    def state_dict_for_save_checkpoint(self, *args, **kwargs):
        r'''
        Keep consitency with Megatron
        '''
        return self.module.state_dict_for_save_checkpoint(*args, **kwargs)

    def load_state_dict(self, *args, **kwargs):
        r'''
        Keep consitency with Megatron
        '''
        return self.module.load_state_dict(*args, **kwargs)
