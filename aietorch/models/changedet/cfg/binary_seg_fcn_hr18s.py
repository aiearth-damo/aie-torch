# model settings
norm_cfg = dict(type='SyncBN', requires_grad=True)
model = dict(
    type='EncoderDecoderBinary',
    pretrained='open-mmlab://msra/hrnetv2_w18_small',
    backbone=dict(
        type='HRNet',
        norm_cfg=norm_cfg,
        norm_eval=False,
        extra=dict(
            stage1=dict(
                num_modules=1,
                num_branches=1,
                block='BOTTLENECK',
                num_blocks=(2, ),
                num_channels=(64, )),
            stage2=dict(
                num_modules=1,
                num_branches=2,
                block='BASIC',
                num_blocks=(2, 2),
                num_channels=(18, 36)),
            stage3=dict(
                num_modules=3,
                num_branches=3,
                block='BASIC',
                num_blocks=(2, 2, 2),
                num_channels=(18, 36, 72)),
            stage4=dict(
                num_modules=2,
                num_branches=4,
                block='BASIC',
                num_blocks=(2, 2, 2, 2),
                num_channels=(18, 36, 72, 144)))),
    decode_head=dict(
        type='FCNHead',
        in_channels=[18, 36, 72, 144],
        in_index=(0, 1, 2, 3),
        channels=sum([18, 36, 72, 144]),
        input_transform='resize_concat',
        kernel_size=1,
        num_convs=1,
        concat_input=False,
        dropout_ratio=-1,
        num_classes=1,
        norm_cfg=norm_cfg,
        align_corners=False,
        loss_decode=dict(type="EnsembleLoss",
                         loss_dict_list=[
                             dict(type='BCELoss', loss_weight=1.0, removeIgnore=True),
                             dict(type='BinaryDiceLoss', batch=True, loss_weight=1.0, sigmoid=True, iou=True,
                                  smooth=1.0),
                         ])),
    # model training and testing settings
    train_cfg=dict(),
    test_cfg=dict(mode='whole_sigmoid', thresh=0.5))