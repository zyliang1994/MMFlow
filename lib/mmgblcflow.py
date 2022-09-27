# ----------------------------------------------------- #
#    Written by zyliang (zyliang18@mails.jlu.edu.cn)    #
# ----------------------------------------------------- #
import numpy as np
import torch
import torch.nn as nn

import lib.layers as layers
import lib.layers.base as base_layers

from typing import Type, Callable, Tuple, Optional, Set, List, Union

from timm.models.efficientnet_blocks import SqueezeExcite, DepthwiseSeparableConv
from timm.models.layers import drop_path, trunc_normal_, DropPath


ACT_FNS = {
    'softplus': lambda b: nn.Softplus(),
    'elu': lambda b: nn.ELU(inplace=b),
    'swish': lambda b: base_layers.Swish(),
    'lcube': lambda b: base_layers.LipschitzCube(),
    'identity': lambda b: base_layers.Identity(),
    'relu': lambda b: nn.ReLU(inplace=b),
    'glue': lambda b: nn.GELU(),
    'sin': lambda b: base_layers.Sin(),
    'zero': lambda b: base_layers.Zero(),
}


# -------------------------------------------------------------------------------------- #
class MMGbLcBlock(nn.Module):
    """ MMGbLc block composed of MBConv block, Block Attention, and Grid Attention."""
    def __init__(
            self,
            in_channels: int,
            out_channels: int,
            downscale: bool = False,
            num_heads: int = 32,
            grid_window_size: Tuple[int, int] = (7, 7),
            attn_drop: float = 0.,
            drop: float = 0.,
            drop_path: float = 0.,
            mlp_ratio: float = 4.,
            act_layer: Type[nn.Module] = nn.GELU,
            norm_layer: Type[nn.Module] = nn.BatchNorm2d,
            norm_layer_transformer: Type[nn.Module] = nn.LayerNorm
    ) -> None:
        """ Constructor method """
        # Call super constructor
        super(MMGbLcBlock, self).__init__()
        # MMGbLcBlock is included the MBConv, Block Trans and Grid Trans.
        # Init MBConv block -- MobileNet
        self.mb_conv = MBConv(
            in_channels=in_channels,
            out_channels=out_channels,
            downscale=downscale,
            act_layer=act_layer,
            norm_layer=norm_layer,
            drop_path=drop_path
        )
        # Init Block and Grid Transformer
        # Block Transformer
        self.block_transformer = GBTrans(
            in_channels=out_channels,
            partition_function=window_partition,
            reverse_function=window_reverse,
            num_heads=num_heads,
            grid_window_size=grid_window_size,
            attn_drop=attn_drop,
            drop=drop,
            drop_path=drop_path,
            mlp_ratio=mlp_ratio,
            act_layer=act_layer,
            norm_layer=norm_layer_transformer
        )
        # Grid Transformer
        self.grid_transformer = GBTrans(
            in_channels=out_channels,
            partition_function=grid_partition,
            reverse_function=grid_reverse,
            num_heads=num_heads,
            grid_window_size=grid_window_size,
            attn_drop=attn_drop,
            drop=drop,
            drop_path=drop_path,
            mlp_ratio=mlp_ratio,
            act_layer=act_layer,
            norm_layer=norm_layer_transformer
        )

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        output = self.grid_transformer(self.block_transformer(self.mb_conv(input)))   # 顺序融合
        return output


# -------------------------------------------------------------------------------------- #
class MMGbLcStage(nn.Module):
    """ Stage of the MMGbLc."""
    def __init__(
            self,
            depth: int,
            in_channels: int,
            out_channels: int,
            num_heads: int = 32,
            grid_window_size: Tuple[int, int] = (7, 7),
            attn_drop: float = 0.,
            drop: float = 0.,
            drop_path: Union[List[float], float] = 0.,
            mlp_ratio: float = 4.,
            act_layer: Type[nn.Module] = nn.GELU,
            norm_layer: Type[nn.Module] = nn.BatchNorm2d,
            norm_layer_transformer: Type[nn.Module] = nn.LayerNorm
    ) -> None:
        """ Constructor method """
        # Call super constructor, super class is the first put into
        super(MMGbLcStage, self).__init__()
        # Init blocks
        self.blocks = nn.Sequential(*[
            MMGbLcBlock(
                in_channels=in_channels if index == 0 else out_channels,
                out_channels=out_channels,
                downscale=index == 0,
                num_heads=num_heads,
                grid_window_size=grid_window_size,
                attn_drop=attn_drop,
                drop=drop,
                drop_path=drop_path if isinstance(drop_path, float) else drop_path[index],
                mlp_ratio=mlp_ratio,
                act_layer=act_layer,
                norm_layer=norm_layer,
                norm_layer_transformer=norm_layer_transformer
            )
            for index in range(depth)
        ])

    def forward(self, input=torch.Tensor) -> torch.Tensor:
        output = self.blocks(input)
        return output


# -------------------------------------------------------------------------------------- #
class MMGbLc(nn.Module):

    def __init__(
            self,
            in_channels: int = 3,
            depths: Tuple[int, ...] = (2, 2, 5, 2),
            channels: Tuple[int, ...] = (64, 128, 256, 512),
            num_classes: int = 1000,
            embed_dim: int = 64,
            num_heads: int = 32,
            grid_window_size: Tuple[int, int] = (7, 7),
            attn_drop: float = 0.,
            drop=0.,
            drop_path=0.,
            mlp_ratio=4.,
            act_layer=nn.GELU,  # GELU is the only activate layer
            norm_layer=nn.BatchNorm2d,
            norm_layer_transformer=nn.LayerNorm,
            global_pool: str = "avg"
    ) -> None:
        """ Constructor method """
        # Call super constructor
        super(MMGbLc, self).__init__()
        # Check parameters
        assert len(depths) == len(channels), "For each stage a channel dimension must be given."
        assert global_pool in ["avg", "max"], f"Only avg and max is supported but {global_pool} is given"
        # Save parameters
        self.num_classes: int = num_classes
        # Init convolutional stem
        self.stem = nn.Sequential(
            nn.Conv2d(in_channels=in_channels, out_channels=embed_dim, kernel_size=(3, 3), stride=(2, 2),
                      padding=(1, 1)),
            act_layer(),
            nn.Conv2d(in_channels=embed_dim, out_channels=embed_dim, kernel_size=(3, 3), stride=(1, 1),
                      padding=(1, 1)),
            act_layer(),
        )
        # Init blocks
        drop_path = torch.linspace(0.0, drop_path, sum(depths)).tolist()
        self.stages = []
        for index, (depth, channel) in enumerate(zip(depths, channels)):
            self.stages.append(
                MMGbLcStage(
                    depth=depth,
                    in_channels=embed_dim if index == 0 else channels[index - 1],
                    out_channels=channel,
                    num_heads=num_heads,
                    grid_window_size=grid_window_size,
                    attn_drop=attn_drop,
                    drop=drop,
                    drop_path=drop_path[sum(depths[:index]):sum(depths[:index + 1])],
                    mlp_ratio=mlp_ratio,
                    act_layer=act_layer,
                    norm_layer=norm_layer,
                    norm_layer_transformer=norm_layer_transformer
                )
            )
        self.global_pool: str = global_pool
        self.head = nn.Linear(channels[-1], num_classes)

    @torch.jit.ignore
    def no_weight_decay(self) -> Set[str]:
        nwd = set()
        for n, _ in self.named_parameters():
            if "relative_position_bias_table" in n:
                nwd.add(n)
        return nwd

    def reset_classifier(self, num_classes: int, global_pool: Optional[str] = None) -> None:
        self.num_classes: int = num_classes
        if global_pool is not None:
            self.global_pool = global_pool
        self.head = nn.Linear(self.num_features, num_classes) if num_classes > 0 else nn.Identity()

    def forward_features(self, input: torch.Tensor) -> torch.Tensor:
        output = input
        for stage in self.stages:
            output = stage(output)
        return output

    def forward_head(self, input: torch.Tensor, pre_logits: bool = False):
        if self.global_pool == "avg":
            input = input.mean(dim=(2, 3))
        elif self.global_pool == "max":
            input = torch.amax(input, dim=(2, 3))
        return input if pre_logits else self.head(input)

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        output = self.forward_features(self.stem(input))
        output = self.forward_head(output)
        return output


# -------------------------------------------------------------------------------------- #
class MMGbLcFlow(nn.Module):

    def __init__(
            self,
            input_size,
            n_blocks: list[int, int] = [16, 16],
            intermediate_dim: int = 64,
            factor_out: bool = True,
            quadratic: bool = False,
            init_layer=None,
            actnorm: bool = False,
            fc_actnorm: bool = False,
            batchnorm: bool = False,
            dropout: int = 0,
            fc: bool = False,
            coeff: float = 0.9,
            vnorms: str = '122f',
            n_lipschitz_iters=None,
            sn_atol=None,
            sn_rtol=None,
            n_power_series: int = 5,
            n_dist: str = 'geometric',
            n_samples: int = 1,
            kernels: str = '3-1-3',
            activation_fn: str = 'elu',
            fc_end: bool = True,
            fc_idim: int = 128,
            n_exact_terms: int = 0,
            preact: bool = False,
            neumann_grad: bool = True,
            grad_in_forward: bool = False,
            first_mmgblcblock: bool = False,
            learn_p: bool = False,
            classification: bool = False,
            classification_hdim: int = 64,
            n_classes: int = 10,
            block_type: str = 'mmgblcblock',
    ):
        super(MMGbLcFlow, self).__init__()
        self.n_scale: int = min(len(n_blocks), self._calc_n_scale(input_size))
        self.n_blocks: list[int, int] = n_blocks
        self.intermediate_dim: int = intermediate_dim
        self.factor_out: bool = factor_out
        self.quadratic: bool = quadratic
        self.init_layer = init_layer
        self.actnorm: bool = actnorm
        self.fc_actnorm: bool = fc_actnorm
        self.batchnorm: bool = batchnorm
        self.dropout: int = dropout
        self.fc: bool = fc
        self.coeff: float = coeff
        self.vnorms: str = vnorms
        self.n_lipschitz_iters = n_lipschitz_iters
        self.sn_atol = sn_atol
        self.sn_rtol = sn_rtol
        self.n_power_series: int = n_power_series
        self.n_dist: str = n_dist
        self.n_samples: int = n_samples
        self.kernels: str = kernels
        self.activation_fn: str = activation_fn
        self.fc_end: bool = fc_end
        self.fc_idim: int = fc_idim
        self.n_exact_terms: int = n_exact_terms
        self.preact: bool = preact
        self.neumann_grad: bool = neumann_grad
        self.grad_in_forward: bool = grad_in_forward
        self.first_mmgblcblock: bool = first_mmgblcblock
        self.learn_p: bool = learn_p
        self.classification: bool = classification
        self.classification_hdim: int = classification_hdim
        self.n_classes: int = n_classes
        self.block_type: str = block_type

        if not self.n_scale > 0:
            raise ValueError('Could not compute number of scales for input of' 'size (%d,%d,%d,%d)' % input_size)

        self.transforms = self._build_net(input_size)
        self.dims = [o[1:] for o in self.calc_output_size(input_size)]

        if self.classification:
            self.build_multiscale_classifier(input_size)

    def _build_net(self, input_size):
        _, c, h, w = input_size
        transforms = []
        _stacked_blocks = StackedMMGbLcBlocks if self.block_type == 'mmgblcblock' else StackedCouplingBlocks
        for i in range(self.n_scale):
            transforms.append(
                _stacked_blocks(
                    initial_size=(c, h, w),
                    idim=self.intermediate_dim,
                    squeeze=(i < self.n_scale - 1),  # don't squeeze last layer
                    init_layer=self.init_layer if i == 0 else None,
                    n_blocks=self.n_blocks[i],
                    quadratic=self.quadratic,
                    actnorm=self.actnorm,
                    fc_actnorm=self.fc_actnorm,
                    batchnorm=self.batchnorm,
                    dropout=self.dropout,
                    fc=self.fc,
                    coeff=self.coeff,
                    vnorms=self.vnorms,
                    n_lipschitz_iters=self.n_lipschitz_iters,
                    sn_atol=self.sn_atol,
                    sn_rtol=self.sn_rtol,
                    n_power_series=self.n_power_series,
                    n_dist=self.n_dist,
                    n_samples=self.n_samples,
                    kernels=self.kernels,
                    activation_fn=self.activation_fn,
                    fc_end=self.fc_end,
                    fc_idim=self.fc_idim,
                    n_exact_terms=self.n_exact_terms,
                    preact=self.preact,
                    neumann_grad=self.neumann_grad,
                    grad_in_forward=self.grad_in_forward,
                    first_mmgblcblock=self.first_mmgblcblock and (i == 0),
                    learn_p=self.learn_p,
                )
            )
            c, h, w = c * 2 if self.factor_out else c * 4, h // 2, w // 2
        return nn.ModuleList(transforms)

    def _calc_n_scale(self, input_size):
        _, _, h, w = input_size
        n_scale = 0
        while h >= 4 and w >= 4:
            n_scale += 1
            h = h // 2
            w = w // 2
        return n_scale

    def calc_output_size(self, input_size):
        n, c, h, w = input_size
        if not self.factor_out:
            k = self.n_scale - 1
            return [[n, c * 4 ** k, h // 2 ** k, w // 2 ** k]]
        output_sizes = []
        for i in range(self.n_scale):
            if i < self.n_scale - 1:
                c *= 2
                h //= 2
                w //= 2
                output_sizes.append((n, c, h, w))
            else:
                output_sizes.append((n, c, h, w))
        return tuple(output_sizes)

    def build_multiscale_classifier(self, input_size):
        n, c, h, w = input_size
        hidden_shapes = []
        for i in range(self.n_scale):
            if i < self.n_scale - 1:
                c *= 2 if self.factor_out else 4
                h //= 2
                w //= 2
            hidden_shapes.append((n, c, h, w))

        classification_heads = []
        for i, hshape in enumerate(hidden_shapes):
            classification_heads.append(
                nn.Sequential(
                    nn.Conv2d(hshape[1], self.classification_hdim, 3, 1, 1),
                    layers.ActNorm2d(self.classification_hdim),
                    nn.ReLU(inplace=True),
                    nn.AdaptiveAvgPool2d((1, 1)),
                )
            )

        self.classification_heads = nn.ModuleList(classification_heads)
        self.logit_layer = nn.Linear(self.classification_hdim * len(classification_heads), self.n_classes)

    def forward(self, x, logpx=None, inverse=False, classify=False):
        if inverse:
            return self.inverse(x, logpx)
        out = []
        if classify: class_outs = []
        for idx in range(len(self.transforms)):
            if logpx is not None:
                x, logpx = self.transforms[idx].forward(x, logpx)
            else:
                x = self.transforms[idx].forward(x)
            if self.factor_out and (idx < len(self.transforms) - 1):
                d = x.size(1) // 2
                x, f = x[:, :d], x[:, d:]
                out.append(f)

            # Handle classification.
            if classify:
                if self.factor_out:
                    class_outs.append(self.classification_heads[idx](f))
                else:
                    class_outs.append(self.classification_heads[idx](x))
        out.append(x)
        out = torch.cat([o.view(o.size()[0], -1) for o in out], 1)
        output = out if logpx is None else (out, logpx)
        if classify:
            h = torch.cat(class_outs, dim=1).squeeze(-1).squeeze(-1)
            logits = self.logit_layer(h)
            return output, logits
        else:
            return output

    def inverse(self, z, logpz=None):
        if self.factor_out:
            z = z.view(z.shape[0], -1)
            zs = []
            i = 0
            for dims in self.dims:
                s = np.prod(dims)
                zs.append(z[:, i:i + s])
                i += s
            zs = [_z.view(_z.size()[0], *zsize) for _z, zsize in zip(zs, self.dims)]

            if logpz is None:
                z_prev = self.transforms[-1].inverse(zs[-1])
                for idx in range(len(self.transforms) - 2, -1, -1):
                    z_prev = torch.cat((z_prev, zs[idx]), dim=1)
                    z_prev = self.transforms[idx].inverse(z_prev)
                return z_prev
            else:
                z_prev, logpz = self.transforms[-1].inverse(zs[-1], logpz)
                for idx in range(len(self.transforms) - 2, -1, -1):
                    z_prev = torch.cat((z_prev, zs[idx]), dim=1)
                    z_prev, logpz = self.transforms[idx].inverse(z_prev, logpz)
                return z_prev, logpz
        else:
            z = z.view(z.shape[0], *self.dims[-1])
            for idx in range(len(self.transforms) - 1, -1, -1):
                if logpz is None:
                    z = self.transforms[idx].inverse(z)
                else:
                    z, logpz = self.transforms[idx].inverse(z, logpz)
            return z if logpz is None else (z, logpz)


# -------------------------------------------------------------------------------------- #
class StackedMMGbLcBlocks(layers.SequentialFlow):

    def __init__(
            self,
            initial_size,
            idim,
            squeeze=True,
            init_layer=None,
            n_blocks=1,
            quadratic=False,
            actnorm=False,
            fc_actnorm=False,
            batchnorm=False,
            dropout=0,
            fc=False,
            coeff=0.9,
            vnorms='122f',
            n_lipschitz_iters=None,
            sn_atol=None,
            sn_rtol=None,
            n_power_series=5,
            n_dist='geometric',
            n_samples=1,
            kernels='3-1-3',
            activation_fn='elu',
            fc_end=True,
            fc_nblocks=2,
            fc_idim=128,
            n_exact_terms=0,
            preact=False,
            neumann_grad=True,
            grad_in_forward=False,
            first_mmgblcblock=False,
            learn_p=False,
    ):
        chain = []
        # Parse vnorms
        ps = []
        for p in vnorms:
            if p == 'f':
                ps.append(float('inf'))
            else:
                ps.append(float(p))
        domains, codomains = ps[:-1], ps[1:]
        assert len(domains) == len(kernels.split('-'))

        def _actnorm(size, fc):
            if fc:
                return FCWrapper(layers.ActNorm1d(size[0] * size[1] * size[2]))
            else:
                return layers.ActNorm2d(size[0])

        def _quadratic_layer(initial_size, fc):   # 定性层，求逆
            if fc:
                c, h, w = initial_size
                dim = c * h * w
                return FCWrapper(layers.InvertibleLinear(dim))
            else:
                return layers.InvertibleConv2d(initial_size[0])

        def _lipschitz_layer(fc):
            return base_layers.get_linear if fc else base_layers.get_conv2d

        def _mmgblcblock(initial_size, fc, idim=idim, first_mmgblcblock=False):
            if fc:
                return layers.MMGbLcBlock(
                    FCNet(
                        input_shape=initial_size,
                        idim=idim,
                        lipschitz_layer=_lipschitz_layer(True),
                        nhidden=len(kernels.split('-')) - 1,
                        coeff=coeff,
                        domains=domains,
                        codomains=codomains,
                        n_iterations=n_lipschitz_iters,
                        activation_fn=activation_fn,
                        preact=preact,
                        dropout=dropout,
                        sn_atol=sn_atol,
                        sn_rtol=sn_rtol,
                        learn_p=learn_p,
                    ),
                    FCNet(
                        input_shape=initial_size,
                        idim=idim,
                        lipschitz_layer=_lipschitz_layer(True),
                        nhidden=len(kernels.split('-')) - 1,
                        coeff=coeff,
                        domains=domains,
                        codomains=codomains,
                        n_iterations=n_lipschitz_iters,
                        activation_fn=activation_fn,
                        preact=preact,
                        dropout=dropout,
                        sn_atol=sn_atol,
                        sn_rtol=sn_rtol,
                        learn_p=learn_p,
                    ),
                    n_power_series=n_power_series,
                    n_dist=n_dist,
                    n_samples=n_samples,
                    n_exact_terms=n_exact_terms,
                    neumann_grad=neumann_grad,
                    grad_in_forward=grad_in_forward,
                )
            else:
                def build_nnet():
                    ks = list(map(int, kernels.split('-')))
                    if learn_p:
                        _domains = [nn.Parameter(torch.tensor(0.)) for _ in range(len(ks))]
                        _codomains = _domains[1:] + [_domains[0]]
                    else:
                        _domains = domains
                        _codomains = codomains
                    nnet = []
                    if not first_mmgblcblock and preact:
                        if batchnorm: nnet.append(layers.MovingBatchNorm2d(initial_size[0]))
                        nnet.append(ACT_FNS[activation_fn](False))
                    nnet.append(
                        _lipschitz_layer(fc)(
                            initial_size[0], idim, ks[0], 1, ks[0] // 2, coeff=coeff, n_iterations=n_lipschitz_iters,
                            domain=_domains[0], codomain=_codomains[0], atol=sn_atol, rtol=sn_rtol
                        )
                    )
                    if batchnorm: nnet.append(layers.MovingBatchNorm2d(idim))
                    nnet.append(ACT_FNS[activation_fn](True))
                    for i, k in enumerate(ks[1:-1]):
                        nnet.append(
                            _lipschitz_layer(fc)(
                                idim, idim, k, 1, k // 2, coeff=coeff, n_iterations=n_lipschitz_iters,
                                domain=_domains[i + 1], codomain=_codomains[i + 1], atol=sn_atol, rtol=sn_rtol
                            )
                        )
                        if batchnorm: nnet.append(layers.MovingBatchNorm2d(idim))
                        nnet.append(ACT_FNS[activation_fn](True))
                    if dropout: nnet.append(nn.Dropout2d(dropout, inplace=True))
                    nnet.append(
                        _lipschitz_layer(fc)(
                            idim, initial_size[0], ks[-1], 1, ks[-1] // 2, coeff=coeff, n_iterations=n_lipschitz_iters,
                            domain=_domains[-1], codomain=_codomains[-1], atol=sn_atol, rtol=sn_rtol
                        )
                    )
                    if batchnorm: nnet.append(layers.MovingBatchNorm2d(initial_size[0]))
                    return nn.Sequential(*nnet)

                return layers.MMGbLcBlock(
                    build_nnet(),
                    build_nnet(),
                    n_power_series=n_power_series,
                    n_dist=n_dist,
                    n_samples=n_samples,
                    n_exact_terms=n_exact_terms,
                    neumann_grad=neumann_grad,
                    grad_in_forward=grad_in_forward,
                )
        if init_layer is not None: chain.append(init_layer)
        if first_mmgblcblock and actnorm: chain.append(_actnorm(initial_size, fc))
        if first_mmgblcblock and fc_actnorm: chain.append(_actnorm(initial_size, True))
        if squeeze:
            c, h, w = initial_size
            for i in range(n_blocks):
                if quadratic: chain.append(_quadratic_layer(initial_size, fc))
                chain.append(_mmgblcblock(initial_size, fc, first_mmgblcblock=first_mmgblcblock and (i == 0)))
                if actnorm: chain.append(_actnorm(initial_size, fc))
                if fc_actnorm: chain.append(_actnorm(initial_size, True))
            chain.append(layers.SqueezeLayer(2))
        else:
            for i in range(n_blocks):
                if quadratic: chain.append(_quadratic_layer(initial_size, fc))
                chain.append(_mmgblcblock(initial_size, fc, first_mmgblcblock=first_mmgblcblock and (i == 0)))
                if actnorm: chain.append(_actnorm(initial_size, fc))
                if fc_actnorm: chain.append(_actnorm(initial_size, True))
            # Use four fully connected layers at the end.
            if fc_end:
                for _ in range(fc_nblocks):
                    chain.append(_mmgblcblock(initial_size, True, fc_idim))
                    if actnorm or fc_actnorm: chain.append(_actnorm(initial_size, True))

        super(StackedMMGbLcBlocks, self).__init__(chain)


# -------------------------------------------------------------------------------------- #
class FCNet(nn.Module):
    def __init__(
            self, input_shape, idim, lipschitz_layer, nhidden, coeff, domains, codomains, n_iterations, activation_fn,
            preact, dropout, sn_atol, sn_rtol, learn_p, div_in=1):
        super(FCNet, self).__init__()
        self.input_shape = input_shape
        c, h, w = self.input_shape
        dim = c * h * w
        nnet = []
        last_dim = dim // div_in
        if preact: nnet.append(ACT_FNS[activation_fn](False))
        if learn_p:
            domains = [nn.Parameter(torch.tensor(0.)) for _ in range(len(domains))]
            codomains = domains[1:] + [domains[0]]
        for i in range(nhidden):
            nnet.append(
                lipschitz_layer(last_dim, idim) if lipschitz_layer == nn.Linear else lipschitz_layer(
                    last_dim, idim, coeff=coeff, n_iterations=n_iterations, domain=domains[i], codomain=codomains[i],
                    atol=sn_atol, rtol=sn_rtol
                )
            )
            nnet.append(ACT_FNS[activation_fn](True))
            last_dim = idim
        if dropout: nnet.append(nn.Dropout(dropout, inplace=True))
        nnet.append(
            lipschitz_layer(last_dim, dim) if lipschitz_layer == nn.Linear else lipschitz_layer(
                last_dim, dim, coeff=coeff, n_iterations=n_iterations, domain=domains[-1], codomain=codomains[-1],
                atol=sn_atol, rtol=sn_rtol
            )
        )
        self.nnet = nn.Sequential(*nnet)

    def forward(self, x, restore=False):
        x = x.view(x.shape[0], -1)
        y = self.nnet(x)
        return y.view(y.shape[0], *self.input_shape)


# -------------------------------------------------------------------------------------- #
class FCWrapper(nn.Module):
    def __init__(self, fc_module):
        super(FCWrapper, self).__init__()
        self.fc_module = fc_module

    def forward(self, x, logpx=None, restore=False):
        shape = x.shape
        x = x.view(x.shape[0], -1)
        if logpx is None:
            y = self.fc_module(x)
            return y.view(*shape)
        else:
            y, logpy = self.fc_module(x, logpx)
            return y.view(*shape), logpy

    def inverse(self, y, logpy=None):
        shape = y.shape
        y = y.view(y.shape[0], -1)
        if logpy is None:
            x = self.fc_module.inverse(y)
            return x.view(*shape)
        else:
            x, logpx = self.fc_module.inverse(y, logpy)
            return x.view(*shape), logpx


# -------------------------------------------------------------------------------------- #
class StackedCouplingBlocks(layers.SequentialFlow):

    def __init__(
            self,
            initial_size,
            idim,
            squeeze=True,
            init_layer=None,
            n_blocks=1,
            quadratic=False,
            actnorm=False,
            fc_actnorm=False,
            batchnorm=False,
            dropout=0,
            fc=False,
            coeff=0.9,
            vnorms='122f',
            n_lipschitz_iters=None,
            sn_atol=None,
            sn_rtol=None,
            n_power_series=5,
            n_dist='geometric',
            n_samples=1,
            kernels='3-1-3',
            activation_fn='elu',
            fc_end=True,
            fc_nblocks=4,
            fc_idim=128,
            n_exact_terms=0,
            preact=False,
            neumann_grad=True,
            grad_in_forward=False,
            first_mmgblcblock=False,
            learn_p=False,
    ):
        # yapf: disable
        class nonloc_scope:
            pass

        nonloc_scope.swap = True
        # yapf: enable
        chain = []

        def _actnorm(size, fc):
            if fc:
                return FCWrapper(layers.ActNorm1d(size[0] * size[1] * size[2]))
            else:
                return layers.ActNorm2d(size[0])

        def _quadratic_layer(initial_size, fc):
            if fc:
                c, h, w = initial_size
                dim = c * h * w
                return FCWrapper(layers.InvertibleLinear(dim))
            else:
                return layers.InvertibleConv2d(initial_size[0])

        def _weight_layer(fc):
            return nn.Linear if fc else nn.Conv2d

        def _mmgblcblock(initial_size, fc, idim=idim, first_mmgblcblock=False):
            if fc:
                nonloc_scope.swap = not nonloc_scope.swap
                return layers.CouplingBlock(
                    initial_size[0],
                    FCNet(
                        input_shape=initial_size,
                        idim=idim,
                        lipschitz_layer=_weight_layer(True),
                        nhidden=len(kernels.split('-')) - 1,
                        activation_fn=activation_fn,
                        preact=preact,
                        dropout=dropout,
                        coeff=None,
                        domains=None,
                        codomains=None,
                        n_iterations=None,
                        sn_atol=None,
                        sn_rtol=None,
                        learn_p=None,
                        div_in=2,
                    ),
                    swap=nonloc_scope.swap,
                )
            else:
                ks = list(map(int, kernels.split('-')))
                if init_layer is None:
                    _block = layers.ChannelCouplingBlock
                    _mask_type = 'channel'
                    div_in = 2
                    mult_out = 1
                else:
                    _block = layers.MaskedCouplingBlock
                    _mask_type = 'checkerboard'
                    div_in = 1
                    mult_out = 2

                nonloc_scope.swap = not nonloc_scope.swap
                _mask_type += '1' if nonloc_scope.swap else '0'

                nnet = []
                if not first_mmgblcblock and preact:
                    if batchnorm: nnet.append(layers.MovingBatchNorm2d(initial_size[0]))
                    nnet.append(ACT_FNS[activation_fn](False))
                nnet.append(_weight_layer(fc)(initial_size[0] // div_in, idim, ks[0], 1, ks[0] // 2))
                if batchnorm: nnet.append(layers.MovingBatchNorm2d(idim))
                nnet.append(ACT_FNS[activation_fn](True))
                for i, k in enumerate(ks[1:-1]):
                    nnet.append(_weight_layer(fc)(idim, idim, k, 1, k // 2))
                    if batchnorm: nnet.append(layers.MovingBatchNorm2d(idim))
                    nnet.append(ACT_FNS[activation_fn](True))
                if dropout: nnet.append(nn.Dropout2d(dropout, inplace=True))
                nnet.append(_weight_layer(fc)(idim, initial_size[0] * mult_out, ks[-1], 1, ks[-1] // 2))
                if batchnorm: nnet.append(layers.MovingBatchNorm2d(initial_size[0]))
                return _block(initial_size[0], nn.Sequential(*nnet), mask_type=_mask_type)

        if init_layer is not None: chain.append(init_layer)
        if first_mmgblcblock and actnorm: chain.append(_actnorm(initial_size, fc))
        if first_mmgblcblock and fc_actnorm: chain.append(_actnorm(initial_size, True))
        if squeeze:
            c, h, w = initial_size
            for i in range(n_blocks):
                if quadratic: chain.append(_quadratic_layer(initial_size, fc))
                chain.append(_mmgblcblock(initial_size, fc, first_mmgblcblock=first_mmgblcblock and (i == 0)))
                if actnorm: chain.append(_actnorm(initial_size, fc))
                if fc_actnorm: chain.append(_actnorm(initial_size, True))
            chain.append(layers.SqueezeLayer(2))
        else:
            for _ in range(n_blocks):
                if quadratic: chain.append(_quadratic_layer(initial_size, fc))
                chain.append(_mmgblcblock(initial_size, fc))
                if actnorm: chain.append(_actnorm(initial_size, fc))
                if fc_actnorm: chain.append(_actnorm(initial_size, True))
            # Use four fully connected layers at the end.
            if fc_end:
                for _ in range(fc_nblocks):
                    chain.append(_mmgblcblock(initial_size, True, fc_idim))
                    if actnorm or fc_actnorm: chain.append(_actnorm(initial_size, True))
        super(StackedCouplingBlocks, self).__init__(chain)


# -------------------------------------------------------------------------------------- #
if __name__ == '__main__':

    def test_block() -> None:
        block = MMGbLcBlock(in_channels=128, out_channels=256, downscale=True)
        input = torch.rand(1, 128, 28, 28)
        output = block(input)
        print(output.shape)

    def test_networks() -> None:
        network = MMGbLc(depths=(2, 6, 14, 2), channels=(96, 192, 384, 768), embed_dim=64, num_classes=365)
        input = torch.rand(1, 3, 224, 224)
        output = network(input)
        print(output.shape)    # [1, 365]

    def test_StackedMMGbLcBlocks() -> None:
        input = torch.rand(1, 3, 64, 64)
        _, c, h, w = input.shape
        initial_size = (c, h, w)
        stackmodel = StackedMMGbLcBlocks(initial_size=initial_size, idim=512)

    def test_MMGbLcFlow() -> None:
        input = torch.rand(1, 3, 64, 64)
        flowmodel = MMGbLcFlow(input_size=input.shape, batchnorm=True, block_type='mmgblcblock',
                               sn_atol=1e-3, sn_rtol=1e-3)
        output = flowmodel(input)
        print(output.shape)

    # UnitTest
    # test_partition_and_revers()
    # test_transformer_block()
    # test_block()
    # test_networks()
    # test_MMGbLcFlow()


"""
ViT常依赖广泛的模型预训练，得益于trans的建模能力，但是缺少CNN的归纳偏置，导致容易过拟合，不容易得到较好的图像识别效果。
进而，引出控制模型容量，提高模型可扩展性的方法，可以实现在参数量减少的同时提高性能，比如Twins、LocalViT、Swin-T。
CNN感受野有限导致很难捕获全局信息，而Transformer可以捕获长距离依赖关系，因此ViT出现之后有许多工作尝试将CNN和Transformer结合，
使得网络结构能够继承CNN和Transformer的优点，并且最大程度保留全局和局部特征。
Transformer 理论上比CNN能得到更好的模型表现，但是因为计算全局注意力导致巨大的计算损失，特别是在浅层网络中，特征图越大，计算复杂度越高，
因此一些方法提出将Transformer插入到CNN主干网络中，或者使用一个Transformer模块替代某一个卷积模块。
BoTNet通过使用Multi-Head Self-Attention(MHSA)替代ResNet Bottleneck中的3×3卷积，其他没有任何改变，形成新的网络结构，
称为Bottleneck Transformer，相比于ResNet等网络提高了在分类，目标检测等任务中的表现。
CoAtNet是首先将CNN与Attention进行结合的，弥补CNN建模能力与ViT不考虑归纳偏置(平移不变性和局部相关性)导致的泛化性较低的不足。
"""
