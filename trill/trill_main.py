import importlib

import pytorch_lightning as pl
import torch
import argparse
import esm
import time
import gc
import subprocess
import os
from git import Repo
from torch import inf
import sys
import xgboost as xgb
from sklearn.model_selection import train_test_split
import torch.nn as nn
import torch.nn.functional as F
import pandas as pd
from Bio import SeqIO
import requests
from pytorch_lightning.loggers import TensorBoardLogger
from pytorch_lightning.profilers import PyTorchProfiler
import shutil
from sklearn.metrics import precision_recall_fscore_support
from tqdm import tqdm
import numpy as np
from rdkit import Chem
# from fairscale.nn.data_parallel import FullyShardedDataParallel as FSDP
# from fairscale.nn.wrap import enable_wrap, wrap
import builtins
from pytorch_lightning.utilities.deepspeed import convert_zero_checkpoint_to_fp32_state_dict
from trill.utils.lightning_models import ESM, ProtGPT2, CustomWriter, ESM_Gibbs, ProtT5, ZymCTRL, ProstT5, Custom3DiDataset, Ankh
from trill.utils.update_weights import weights_update
from trill.utils.dock_utils import perform_docking, fixer_of_pdbs, write_docking_results_to_file
from trill.utils.simulation_utils import relax_structure, run_simulation
from transformers import AutoTokenizer, EsmForProteinFolding, set_seed
from pytorch_lightning.callbacks import ModelCheckpoint
# from trill.utils.strategy_tuner import tune_esm_inference, tune_esm_train
from trill.utils.protgpt2_utils import ProtGPT2_wrangle
from trill.utils.esm_utils import ESM_IF1_Wrangle, ESM_IF1, convert_outputs_to_pdb, parse_and_save_all_predictions
from trill.utils.visualize import reduce_dims, viz
from trill.utils.MLP import MLP_C2H2, inference_epoch
from sklearn.ensemble import IsolationForest
import skops.io as sio
from sklearn.preprocessing import LabelEncoder
import trill.utils.ephod_utils as eu
from trill.utils.classify_utils import generate_class_key_csv, prep_data, log_results, xg_test, sweep, train_model, custom_xg_test
from trill.utils.fetch_embs import convert_embeddings_to_csv, download_embeddings
from esm.inverse_folding.util import load_coords
import logging
from pyfiglet import Figlet
import bokeh
from Bio import PDB
from icecream import ic
import pkg_resources

os.environ['CUDA_LAUNCH_BLOCKING'] = '1'
os.environ["TOKENIZERS_PARALLELISM"] = "false"


def main(args):

    # torch.set_float32_matmul_precision('medium')
    start = time.time()
    f = Figlet(font="graffiti")
    print(f.renderText("TRILL"))
    
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "name",
        help = "Name of run",
        action = "store"
        )

    
    parser.add_argument(
        "GPUs",
        help="Input total number of GPUs per node",
        action="store",
        default = 1
)

    parser.add_argument(
        "--nodes",
        help="Input total number of nodes. Default is 1",
        action="store",
        default = 1
)
    

    parser.add_argument(
        "--logger",
        help="Enable Tensorboard logger. Default is None",
        action="store",
        default = False,
        dest="logger",
)

    parser.add_argument(
        "--profiler",
        help="Utilize PyTorchProfiler",
        action="store_true",
        default=False,
        dest="profiler",
)
    parser.add_argument(
        "--RNG_seed",
        help="Input RNG seed. Default is 123",
        action="store",
        default = 123
)
    parser.add_argument(
        "--outdir",
        help="Input full path to directory where you want the output from TRILL",
        action="store",
        default = '.'
)

    parser.add_argument(
        "--n_workers",
        help="Change number of CPU cores/'workers' TRILL uses",
        action="store",
        default = 1
)


##############################################################################################################

    subparsers = parser.add_subparsers(dest='command')

    commands = {}
    for command in [
        "embed",
        "finetune",
        "inv_fold_gen",
        "lang_gen",
        "diff_gen",
        "classify",
        "fold",
        "visualize",
        "simulate",
    ]:
        commands[command] = importlib.import_module(f"trill.commands.{command}")
        commands[command].setup(subparsers)


##############################################################################################################
    dock = subparsers.add_parser('dock', help='Perform molecular docking with proteins and ligands. Note that you should relax your protein receptor with Simulate or another method before docking.')

    dock.add_argument("algorithm",
        help="Note that while LightDock can dock protein ligands, DiffDock, Smina, and Vina can only do small-molecules.",
        choices = ['DiffDock', 'Vina', 'Smina', 'LightDock', 'GeoDock']
    )

    dock.add_argument("protein", 
        help="Protein of interest to be docked with ligand", 
        action="store"
        )
    
    dock.add_argument("ligand", 
        help="Ligand to dock protein with. Note that with Autodock Vina, you can dock multiple ligands at one time. Simply provide them one after another before any other optional TRILL arguments are added. Also, if a .txt file is provided with each line providing the absolute path to different ligands, TRILL will dock each ligand one at a time.", 
        action="store",
        nargs='*'
        )
    
    # dock.add_argument("--force_ligand", 
    #     help="If you are not doing blind docking, TRILL will automatically assume your ligand is a small molecule if the MW is less than 800. To get around this, you can force TRILL to read the ligand as either type.", 
    #     default=False,
    #     choices = ['small', 'protein']
    #     )
    
    dock.add_argument("--save_visualisation", 
        help="DiffDock: Save a pdb file with all of the steps of the reverse diffusion.", 
        action="store_true",
        default=False
        )
    
    dock.add_argument("--samples_per_complex", 
        help="DiffDock: Number of samples to generate.", 
        type = int,
        action="store",
        default=10
        )
    
    dock.add_argument("--no_final_step_noise", 
        help="DiffDock: Use no noise in the final step of the reverse diffusion", 
        action="store_true",
        default=False
        )
    
    dock.add_argument("--inference_steps", 
        help="DiffDock: Number of denoising steps", 
        type=int,
        action="store",
        default=20
        )

    dock.add_argument("--actual_steps", 
        help="DiffDock: Number of denoising steps that are actually performed", 
        type=int,
        action="store",
        default=None
        )
    dock.add_argument("--min_radius", 
        help="Smina/Vina + Fpocket: Minimum radius of alpha spheres in a pocket. Default is 3Å.", 
        type=float,
        action="store",
        default=3.0
        )

    dock.add_argument("--max_radius", 
        help="Smina/Vina + Fpocket: Maximum radius of alpha spheres in a pocket. Default is 6Å.", 
        type=float,
        action="store",
        default=6.0
        )

    dock.add_argument("--min_alpha_spheres", 
        help="Smina/Vina + Fpocket: Minimum number of alpha spheres a pocket must contain to be considered. Default is 35.", 
        type=int,
        action="store",
        default=35
        )
    
    dock.add_argument("--exhaustiveness", 
        help="Smina/Vina: Change computational effort.", 
        type=int,
        action="store",
        default=8
        )
    
    dock.add_argument("--blind", 
        help="Smina/Vina: Perform blind docking and skip binding pocket prediction with fpocket", 
        action="store_true",
        default=False
        )
    dock.add_argument("--anm", 
        help="LightDock: If selected, backbone flexibility is modeled using Anisotropic Network Model (via ProDy)", 
        action="store_true",
        default=False
        )
    
    dock.add_argument("--swarms", 
        help="LightDock: The number of swarms of the simulations, default is 25", 
        action="store",
        type=int,
        default=25
        )
    
    dock.add_argument("--sim_steps", 
        help="LightDock: The number of steps of the simulation. Default is 100", 
        action="store",
        type=int,
        default=100
        )
    dock.add_argument("--restraints", 
        help="LightDock: If restraints_file is provided, residue restraints will be considered during the setup and the simulation", 
        action="store",
        default=None
        )
##############################################################################################################

    utils = subparsers.add_parser('utils', help='Misc utilities')

    utils.add_argument(
        "tool",
        help="prepare_class_key: Pepare a csv for use with the classify command. Takes a directory or text file with list of paths for fasta files. Each file will be a unique class, so if your directory contains 5 fasta files, there will be 5 classes in the output key csv.",
        choices = ['prepare_class_key', 'fetch_embeddings']
)

    utils.add_argument(
        "--dir",
        help="Directory to be used for creating a class key csv for classification.",
        action="store",
)

    utils.add_argument(
        "--fasta_paths_txt",
        help="Text file with absolute paths of fasta files to be used for creating the class key. Each unique path will be treated as a unique class, and all the sequences in that file will be in the same class.",
        action="store",
)
    utils.add_argument(
    "--uniprotDB",
    help="UniProt embedding dataset to download.",
    choices=['UniProtKB',
        'A.thaliana',
        'C.elegans',
        'E.coli',
        'H.sapiens',
        'M.musculus',
        'R.norvegicus',
        'SARS-CoV-2'],
    action="store",
)   
    utils.add_argument(
    "--rep",
    help="The representation to download.",
    choices=['per_AA', 'avg'],
    action="store"
)

    
##############################################################################################################

    

    args = parser.parse_args()

    home_dir = os.path.expanduser("~")
    cache_dir = os.path.join(home_dir, ".trill_cache")
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)

    pl.seed_everything(int(args.RNG_seed))
    set_seed(int(args.RNG_seed))

    if not os.path.exists(args.outdir):
        os.mkdir(args.outdir)
    
    
    torch.backends.cuda.matmul.allow_tf32 = True
    if int(args.GPUs) == 0:
        os.environ['CUDA_VISIBLE_DEVICES'] = ''
    if int(args.nodes) <= 0:
            raise Exception(f'There needs to be at least one cpu node to use TRILL')
    #if args.tune == True:
        #data = esm.data.FastaBatchedDataset.from_file(args.query)
        # tune_esm_inference(data)
        # tune_esm_train(data, int(args.GPUs))
    
    else:    
        if args.logger == True:
            logger = TensorBoardLogger("logs")
        else:
            logger = False
        if args.profiler:
            profiler = PyTorchProfiler(filename='test-logs')
        else:
            profiler = None

    if args.command in commands:
        commands[args.command].run(args, logger, profiler)
    else:


        if args.command == 'dock':
            ligands = []
            if isinstance(args.ligand, list) and len(args.ligand) > 1:
                for lig in args.ligand:
                    ligands.append(lig)
                    args.multi_lig = True
            else:
                args.ligand = args.ligand[0]
                args.multi_lig = False
                if args.ligand.endswith('.txt'):
                    with open(args.ligand, 'r') as infile:
                        for path in infile:
                            path = path.strip()
                            if not path:
                                continue
                            ligands.append(path)
                else:
                    ligands.append(args.ligand)

            protein_name = os.path.splitext(os.path.basename(args.protein))[0]


            if args.algorithm == 'Smina' or args.algorithm == 'Vina':
                docking_results = perform_docking(args, ligands)
                write_docking_results_to_file(docking_results, args, protein_name, args.algorithm)
            elif args.algorithm == 'LightDock':
                perform_docking(args, ligands)
                print(f"LightDock run complete! Output files are in {args.outdir}")
            elif args.algorithm == 'GeoDock':
                try:
                    pkg_resources.get_distribution('geodock')
                except pkg_resources.DistributionNotFound:
                    install_cmd = 'pip install git+https://github.com/martinez-zacharya/GeoDock.git'.split(' ')
                    subprocess.run(install_cmd)
                from geodock.GeoDockRunner import GeoDockRunner, EnMasseGeoDockRunner
                base_url = "https://raw.githubusercontent.com/martinez-zacharya/GeoDock/main/geodock/weights/dips_0.3.ckpt"
                weights_path = f'{cache_dir}/dips_0.3.ckpt'
                if not os.path.exists(weights_path):
                    r = requests.get(base_url)
                    with open(weights_path, "wb") as file:
                        file.write(r.content)

                rec_coord, rec_seq = load_coords(args.protein, chain=None)
                rec_name = os.path.basename(args.protein).split('.')[0]

                lig_seqs = []
                lig_coords = []
                lig_names = []
                with open(f'tmp_master.fasta', 'w+') as fasta:
                    fasta.write(f'>{rec_name}\n')
                    fasta.write(f'{rec_seq}\n')
                    for lig in ligands:
                        lig_name = os.path.basename(lig).split('.')[0]
                        coords, seq = load_coords(lig, chain=None)
                        coords = torch.nan_to_num(torch.from_numpy(coords))
                        lig_seqs.append(seq)
                        lig_coords.append(coords)
                        lig_names.append(lig_name)
                        fasta.write(f'>{lig_name}\n')
                        fasta.write(f'{seq}\n')

                model_import_name = f'esm.pretrained.esm2_t33_650M_UR50D()'
                args.per_AA = True
                args.avg = False
                model = ESM(eval(model_import_name), 0.0001, args)
                seq_data = esm.data.FastaBatchedDataset.from_file('tmp_master.fasta')
                loader = torch.utils.data.DataLoader(seq_data, shuffle = False, batch_size = 1, num_workers=0, collate_fn=model.alphabet.get_batch_converter())
                pred_writer = CustomWriter(output_dir=args.outdir, write_interval="epoch")
                if int(args.GPUs) == 0:
                    trainer = pl.Trainer(enable_checkpointing=False, callbacks = [pred_writer], logger=logger, num_nodes=int(args.nodes))
                else:
                    trainer = pl.Trainer(enable_checkpointing=False, precision=16, devices=int(args.GPUs), callbacks = [pred_writer], accelerator='gpu', logger=logger, num_nodes=int(args.nodes))

                trainer.predict(model, loader)
                parse_and_save_all_predictions(args)
                master_embs = []
                emb_file = torch.load(f'{args.outdir}/predictions_0.pt')
                for entry in emb_file[0]:
                    emb = entry[0][0][0]
                    master_embs.append(emb)

                rec_emb = master_embs.pop(0)
                for lig_name, lig_seq, lig_coord, lig_emb in zip(lig_names, lig_seqs, lig_coords, master_embs):
                    em_geodock = EnMasseGeoDockRunner(args, ckpt_file=weights_path)
                    pred = em_geodock.dock(
                        rec_info = [rec_name, rec_seq, rec_coord, rec_emb],
                        lig_info = [lig_name, lig_seq, lig_coord, lig_emb],
                        out_name = args.name + '_' + rec_name + '_' + lig_name
                    )
                os.remove(f'{args.outdir}/predictions_0.pt')

            elif args.algorithm == 'DiffDock':
                if not os.path.exists(os.path.join(cache_dir, 'DiffDock')):
                    print('Cloning forked DiffDock')
                    os.makedirs(os.path.join(cache_dir, 'DiffDock'))
                    diffdock = Repo.clone_from('https://github.com/martinez-zacharya/DiffDock', os.path.join(cache_dir, 'DiffDock'))
                    diffdock_root = diffdock.git.rev_parse("--show-toplevel")
                    subprocess.run(['pip', 'install', '-e', diffdock_root])
                    sys.path.insert(0, os.path.join(cache_dir, 'DiffDock'))
                else:
                    sys.path.insert(0, os.path.join(cache_dir, 'DiffDock'))
                    diffdock = Repo(os.path.join(cache_dir, 'DiffDock'))
                    diffdock_root = diffdock.git.rev_parse("--show-toplevel")
                from inference import run_diffdock
                run_diffdock(args, diffdock_root)

                    # out_dir = os.path.join(args.outdir, f'{args.name}_DiffDock_out')
                    # rec = args.protein.split('.')[-2]
                    # out_rec = rec.split('/')[-1]
                    # convert_rec = f'obabel {rec}.pdb -O {out_rec}.pdbqt'.split(' ')
                    # subprocess.run(convert_rec, stdout=subprocess.DEVNULL)
                    # for file in os.listdir(out_dir):
                    #     if 'confidence' in file:
                    #         file_pre = file.split('.sdf')[-2]
                    #         convert_lig = f'obabel {out_dir}/{file} -O {file_pre}.pdbqt'.split(' ')
                    #         subprocess.run(convert_lig, stdout=subprocess.DEVNULL)

                    #         smina_cmd = f'smina --score_only -r {out_rec}.pdbqt -l {file_pre}.pdbqt'.split(' ')
                    #         result = subprocess.run(smina_cmd, stdout=subprocess.PIPE)

                    #         result = re.search("Affinity: \w+.\w+", result.stdout.decode('utf-8'))
                    #         affinity = result.group()
                    #         affinity = re.search('\d+\.\d+', affinity).group()


        elif args.command == 'utils':
            if args.tool == 'prepare_class_key':
                generate_class_key_csv(args)
            elif args.tool == 'fetch_embeddings':
                h5_path = download_embeddings(args)
                h5_name = os.path.splitext(os.path.basename(h5_path))[0]
                convert_embeddings_to_csv(h5_path, os.path.join(args.outdir, f'{h5_name}.csv'))


        



    
    end = time.time()
    print("Finished!")
    print(f"Time elapsed: {end-start} seconds")
 

def cli(args=None):
    if not args:
        args = sys.argv[1:]    
    main(args)
if __name__ == '__main__':
    print("this shouldn't show up...")
