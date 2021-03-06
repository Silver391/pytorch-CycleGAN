import torch
import itertools
from util.image_pool import ImagePool
from .base_model import BaseModel
from . import networks
from . import networks_reid


class ReidcycleGANModel(BaseModel):
    def name(self):
        return 'ReidcycleGANModel'

    @staticmethod
    def modify_commandline_options(parser, is_train=True):
        # default CycleGAN did not use dropout
        parser.set_defaults(no_dropout=True)
        if is_train:
            parser.add_argument('--lambda_A', type=float, default=10.0, help='weight for cycle loss (A -> B -> A)')
            parser.add_argument('--lambda_B', type=float, default=10.0,
                                help='weight for cycle loss (B -> A -> B)')
            parser.add_argument('--lambda_identity', type=float, default=0.5, help='use identity mapping. Setting lambda_identity other than 0 has an effect of scaling the weight of the identity mapping loss. For example, if the weight of the identity loss should be 10 times smaller than the weight of the reconstruction loss, please set lambda_identity = 0.1')
            # reid parameters
            parser.add_argument('--droprate', type=float, default=0.5, help='the dropout ratio in reid model')
            # use feat_loss
            parser.add_argument('--use_feat', action='store_true', help='use feature loss')
            # parser.add_argument('--lambda_feat', type=float, default=1.0, help='weight for feature loss')
            parser.add_argument('--lambda_Rec', type=float, default=10.0, help='weight for reconstruction loss')
        return parser

    def initialize(self, opt):
        BaseModel.initialize(self, opt)

        # specify the training losses you want to print out. The program will call base_model.get_current_losses
        self.loss_names = ['D_A', 'G_A', 'cycle_A', 'idt_A', 'D_B', 'G_B', 'cycle_B', 'idt_B', 'reid']
        # self.loss_names = ['D_A', 'G_A', 'cycle_A', 'idt_A', 'D_B', 'G_B', 'cycle_B', 'idt_B', 'reid', 'Rec']
        if opt.use_feat:
            self.loss_names.append('feat')
        # specify the images you want to save/display. The program will call base_model.get_current_visuals
        visual_names_A = ['real_A', 'fake_B', 'rec_A']
        visual_names_B = ['real_B', 'fake_A', 'rec_B', 'GT_B']
        if self.isTrain and self.opt.lambda_identity > 0.0:
            visual_names_A.append('idt_A')
            visual_names_B.append('idt_B')
        self.visual_names = visual_names_A + visual_names_B

        # specify the models you want to save to the disk. The program will call base_model.save_networks and base_model.load_networks
        if self.isTrain:
            self.model_names = ['G_A', 'G_B', 'D_A', 'D_B', 'D_reid']
        else:  # during test time, only load Gs
            self.model_names = ['G_A', 'G_B', 'D_reid']

        self.num_attr = opt.num_attr
        self.use_feat = opt.use_feat
        # load/define networks
        # The naming conversion is different from those used in the paper
        # Code (paper): G_A (G), G_B (F), D_A (D_Y), D_B (D_X)
        self.netG_A = networks.define_G(opt.input_nc, opt.output_nc, opt.ngf, opt.netG, opt.norm,
                                        not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)
        # Input: B(low-resolution)
        self.netG_B = networks.define_G(opt.output_nc, opt.input_nc, opt.ngf, opt.netG, opt.norm,
                                        not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)

        # Load a pretrained resnet model and reset the final connected layer
        self.netD_reid = networks_reid.ft_net(opt.num_classes, opt.droprate)
        # the reid network is trained on a single gpu because of the BatchNorm layer
        self.netD_reid = self.netD_reid.to(self.device)

        if self.isTrain:
            use_sigmoid = opt.no_lsgan
            self.netD_A = networks.define_D(opt.output_nc, opt.ndf, opt.netD,
                                            opt.n_layers_D, opt.norm, use_sigmoid, opt.init_type, opt.init_gain, self.gpu_ids)

            self.netD_B = networks.define_D(opt.input_nc, opt.ndf, opt.netD,
                                            opt.n_layers_D, opt.norm, use_sigmoid, opt.init_type, opt.init_gain, self.gpu_ids)

        if self.isTrain:
            self.fake_A_pool = ImagePool(opt.pool_size)
            self.fake_B_pool = ImagePool(opt.pool_size)
            # define loss functions
            self.criterionGAN = networks.GANLoss(use_lsgan=not opt.no_lsgan).to(self.device)
            self.criterionCycle = torch.nn.L1Loss()
            self.criterionIdt = torch.nn.L1Loss()
            self.criterionReid = torch.nn.CrossEntropyLoss()
            self.criterionFeat = torch.nn.MSELoss()
            self.criterionRec = torch.nn.L1Loss()
            # initialize optimizers
            self.optimizer_G = torch.optim.Adam(itertools.chain(self.netG_A.parameters(), self.netG_B.parameters()),
                                                lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizer_D = torch.optim.Adam(itertools.chain(self.netD_A.parameters(), self.netD_B.parameters()),
                                                lr=opt.lr, betas=(opt.beta1, 0.999))

            # SR optimizer
            # self.optimizers = []
            self.optimizers.append(self.optimizer_G)
            self.optimizers.append(self.optimizer_D)

            # reid optimizer
            ignored_params = list(map(id, self.netD_reid.model.fc.parameters())) + \
                             list(map(id, self.netD_reid.classifier.parameters()))
            base_params = filter(lambda p: id(p) not in ignored_params, self.netD_reid.parameters())
            self.optimizer_D_reid = torch.optim.SGD([
                {'params': base_params, 'lr': 0.1 * opt.reid_lr},
                {'params': self.netD_reid.model.fc.parameters(), 'lr': opt.reid_lr},
                {'params': self.netD_reid.classifier.parameters(), 'lr': opt.reid_lr}
            ], weight_decay=5e-4, momentum=0.9, nesterov=True)

            self.optimizer_reid.append(self.optimizer_D_reid)
            # Decay learning rate by a factor of 0.1 every 40 epochs
            # self.exp_lr_scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer_D_reid,
            #                                                         step_size=40, gamma=0.1)

    def reset_model_status(self):
        if self.opt.stage==1:
            self.netG_A.train()
            self.netG_B.train()
            self.netD_A.train()
            self.netD_B.train()
            # for the BatchNorm
            self.netD_reid.eval()
        elif self.opt.stage==0 or self.opt.stage==2:
            self.netG_A.train()
            self.netG_B.train()
            self.netD_A.train()
            self.netD_B.train()
            # for the BatchNorm
            self.netD_reid.train()

    def set_input(self, input):
        self.real_A = input['A'].to(self.device)
        self.real_B = input['B'].to(self.device)
        self.image_paths = input['A_paths']

        self.GT_A = input['GT_A'].to(self.device)  # low-resolution
        # load the ground-truth high resolution B image to test the SR quality
        self.GT_B = input['GT_B'].to(self.device)

        # get the id label for person reid
        self.A_label = input['A_label'].to(self.device)
        self.B_label = input['B_label'].to(self.device)
        # print(self.B_real_attr.size())

    def forward(self):
        self.fake_B = self.netG_A(self.real_A)
        self.rec_A = self.netG_B(self.fake_B)

        self.fake_A = self.netG_B(self.real_B)
        self.rec_B = self.netG_A(self.fake_A)

        # person re-id prediction of HR images
        # more strong than the D_B loss
        # self.pred_real_A = self.netD_reid(self.real_A)  # A_label HR
        # self.feat_real_A = self.netD_reid.get_feature()
        # self.pred_fake_A = self.netD_reid(self.fake_A)  # B_label HR
        # self.feat_fake_A = self.netD_reid.get_feature()

        # self.imgs = torch.cat([self.real_A, self.fake_A], 0)
        # self.labels = torch.cat([self.A_label, self.B_label], 0)
        self.imgs = torch.cat([self.real_A, self.fake_A, self.rec_A, self.real_B, self.fake_B, self.rec_B], 0)
        self.labels = torch.cat([self.A_label, self.B_label, self.A_label,
                                 self.B_label, self.A_label, self.B_label])
        self.pred_imgs = self.netD_reid(self.imgs)

    def psnr_eval(self):
        # compute the PSNR for the test
        self.bicubic_psnr = networks.compute_psnr(self.GT_B, self.real_B)
        self.psnr = networks.compute_psnr(self.GT_B, self.fake_A)

    def ssim_eval(self):
        self.bicubic_ssim = networks.compute_ssim(self.GT_B, self.real_B)
        self.ssim = networks.compute_ssim(self.GT_B, self.fake_A)


    def backward_D_basic(self, netD, real, fake):
        # Real
        pred_real = netD(real)
        loss_D_real = self.criterionGAN(pred_real, True)
        # Fake
        pred_fake = netD(fake.detach())
        loss_D_fake = self.criterionGAN(pred_fake, False)
        # Combined loss
        loss_D = (loss_D_real + loss_D_fake) * 0.5
        # backward
        loss_D.backward()
        return loss_D

    def backward_D_A(self):
        fake_B = self.fake_B_pool.query(self.fake_B)
        self.loss_D_A = self.backward_D_basic(self.netD_A, self.real_B, fake_B)

    def backward_D_B(self):
        fake_A = self.fake_A_pool.query(self.fake_A)
        self.loss_D_B = self.backward_D_basic(self.netD_B, self.real_A, fake_A)

    def backward_G(self):
        lambda_idt = self.opt.lambda_identity
        lambda_A = self.opt.lambda_A
        lambda_B = self.opt.lambda_B
        lambda_Rec = self.opt.lambda_Rec
        # Identity loss
        if lambda_idt > 0:
            # G_A should be identity if real_B is fed.
            self.idt_A = self.netG_A(self.real_B)
            self.loss_idt_A = self.criterionIdt(self.idt_A, self.real_B) * lambda_B * lambda_idt
            # G_B should be identity if real_A is fed.
            self.idt_B = self.netG_B(self.real_A)
            self.loss_idt_B = self.criterionIdt(self.idt_B, self.real_A) * lambda_A * lambda_idt
        else:
            self.loss_idt_A = 0
            self.loss_idt_B = 0

        # GAN loss D_A(G_A(A))
        self.loss_G_A = self.criterionGAN(self.netD_A(self.fake_B), True)
        # GAN loss D_B(G_B(B))
        self.loss_G_B = self.criterionGAN(self.netD_B(self.fake_A), True)
        # Forward cycle loss
        self.loss_cycle_A = self.criterionCycle(self.rec_A, self.real_A) * lambda_A
        # Backward cycle loss
        self.loss_cycle_B = self.criterionCycle(self.rec_B, self.real_B) * lambda_B
        # combined loss
        self.loss_G = self.loss_G_A + self.loss_G_B + self.loss_cycle_A + self.loss_cycle_B + self.loss_idt_A + self.loss_idt_B

        # reconstruct loss of low resolution fake_B
        # self.loss_Rec = self.criterionRec(self.fake_B, self.GT_A) * lambda_Rec
        # self.loss_G += self.loss_Rec

        # # add reid loss to update the G_B(LR-HR)
        # _, pred_label_real_A = torch.max(self.pred_real_A, 1)
        # _, pred_label_fake_A = torch.max(self.pred_fake_A, 1)
        # self.corrects_A += float(torch.sum(pred_label_real_A == self.A_label))
        # self.corrects_B += float(torch.sum(pred_label_fake_A == self.B_label))
        #
        # # add reid loss to update the G_B(LR-HR)
        # loss_reid_real_A = self.criterionReid(self.pred_real_A, self.A_label)
        # loss_reid_fake_A = self.criterionReid(self.pred_fake_A, self.B_label)
        # self.loss_reid = loss_reid_real_A + loss_reid_fake_A
        # # pull the features of the same person
        # if self.use_feat:
        #     # print(self.feat_real_A.size(), self.feat_fake_A.size())
        #     self.loss_feat = self.criterionFeat(self.feat_real_A, self.feat_fake_A)
        #     self.loss_reid += self.loss_feat

        _, pred_label_imgs = torch.max(self.pred_imgs, 1)
        self.corrects += float(torch.sum(pred_label_imgs == self.labels))
        self.loss_reid = self.criterionReid(self.pred_imgs, self.labels)

        self.loss_G = self.loss_G + self.loss_reid
        self.loss_G.backward()

    def optimize_parameters(self):
        # forward
        self.forward()
        # print the parameter names
        # params = self.netD_reid.classifier.state_dict()
        # for k, v in params.items():
        #     if 'classifier' in k:
        #         print(k)
        #         print(v)
        if self.opt.stage == 1:
            # G_A and G_B
            # self.set_requires_grad([self.netD_A, self.netD_B], False)
            self.set_requires_grad([self.netD_A, self.netD_B, self.netD_reid], False)
            self.optimizer_G.zero_grad()
            self.backward_G()
            self.optimizer_G.step()
            # D_A and D_B
            self.set_requires_grad([self.netD_A, self.netD_B], True)
            self.optimizer_D.zero_grad()
            self.backward_D_A()
            self.backward_D_B()
            self.optimizer_D.step()
        if self.opt.stage == 0 or self.opt.stage == 2:
            # G_A and G_B
            self.set_requires_grad([self.netD_A, self.netD_B], False)
            self.optimizer_G.zero_grad()
            self.optimizer_D_reid.zero_grad()
            self.backward_G()
            self.optimizer_G.step()
            self.optimizer_D_reid.step()
            # D_A and D_B
            self.set_requires_grad([self.netD_A, self.netD_B], True)
            self.optimizer_D.zero_grad()
            self.backward_D_A()
            self.backward_D_B()
            self.optimizer_D.step()