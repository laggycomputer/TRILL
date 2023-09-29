import os
import subprocess
from rdkit import Chem
import pandas as pd
from openmm.app.pdbfile import PDBFile
from openmm.app import NoCutoff, element
import pdbfixer
from tqdm import tqdm
from multiprocessing import Pool
from functools import partial
import shutil
from biobb_vs.fpocket.fpocket_filter import fpocket_filter
from biobb_vs.fpocket.fpocket_select import fpocket_select
from biobb_vs.fpocket.fpocket_run import fpocket_run
from biobb_vs.utils.box import box
import json
from Bio.PDB import PDBParser, Superimposer, PDBIO
import glob
from io import StringIO 
import sys
from Bio.PDB import PDBParser, Superimposer, PDBIO
import numpy as np


class Capturing(list):
    def __enter__(self):
        self._stdout = sys.stdout
        sys.stdout = self._stringio = StringIO()
        return self
    def __exit__(self, *args):
        self.extend(self._stringio.getvalue().splitlines())
        del self._stringio    # free up some memory
        sys.stdout = self._stdout


def load_molecule(filename, removeHs=False):
    _, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext == '.mol' or ext == '.sdf':
        return Chem.MolFromMolFile(filename, removeHs=removeHs)
    elif ext == '.pdb':
        return Chem.MolFromPDBFile(filename, removeHs=removeHs)
    elif ext == '.mol2':
        return Chem.MolFromMol2File(filename, removeHs=removeHs)
    else:
        raise ValueError(f'Unsupported file format: {ext}')
    
def parse_pocket_output(output):
    matched_pockets = []
    
    for line in output:
        if line.startswith("pocket"):
            matched_pockets.append(line)
        
    return matched_pockets

def convert_protein_to_pdbqt(protein_file, rec_pdbqt):
    print(f'Converting {protein_file} to {rec_pdbqt}...')
    convert_rec = f'obabel -ipdb {protein_file} --partialcharge eem -h -xr -opdbqt -O {rec_pdbqt}'.split(' ')
    subprocess.run(convert_rec, stdout=subprocess.DEVNULL)

def convert_ligand_to_pdbqt(ligand_file, lig_pdbqt, lig_ext):
    print(f'Converting {ligand_file} to {lig_pdbqt}...')
    if lig_ext == "pdb":
        convert_lig = f'obabel -ipdb {ligand_file} -xr -h --partialcharge eem -opdbqt -O {lig_pdbqt}'.split(' ')
    else:
        convert_lig = f'obabel -i{lig_ext} {ligand_file} -h --partialcharge eem -opdbqt -O {lig_pdbqt}'.split(' ')
    subprocess.run(convert_lig, stdout=subprocess.DEVNULL)

def perform_docking(args, ligands):
    protein_file = os.path.abspath(args.protein)
    # ligand_file = os.path.abspath(ligands)
    
    if args.algorithm == 'LightDock':
        lightdock(args, ligands)
        return

    protein_name, prot_ext = os.path.basename(protein_file).split('.')
    # lig_name, lig_ext = os.path.basename(ligand_file).split('.')
    rec_pdbqt = f'{os.path.join(args.outdir, protein_name)}.pdbqt'
    # lig_pdbqt = f'{os.path.join(args.outdir, lig_name)}.pdbqt'

    if prot_ext != 'pdbqt' and args.algorithm != 'Smina':
        convert_protein_to_pdbqt(protein_file, rec_pdbqt)

    if not args.blind:
        run_fpocket_hunting(protein_file, protein_name, args)
        pockets = run_fpocket_filtering(protein_name)
    docking_results = []

    for current_lig in ligands:
        ligand_file = os.path.abspath(current_lig)
        lig_name, lig_ext = os.path.basename(ligand_file).split('.')
        lig_pdbqt = f'{os.path.join(args.outdir, lig_name)}.pdbqt'
        
        if args.algorithm == 'Vina' and lig_ext != 'pdbqt':
            convert_ligand_to_pdbqt(ligand_file, lig_pdbqt, lig_ext)
        
        if not args.blind:
            for pocket in tqdm(pockets, desc=f"Docking {protein_name} and {lig_name}"):
              with Capturing() as output3:
                fpocket_select(f'{protein_name}_filtered_pockets.zip', f'{protein_name}_filtered_pockets/{pocket}_atm.pdb', f'{protein_name}_filtered_pockets/{pocket}_vert.pqr')
                prop = {
                'offset': 2,
                'box_coordinates': True
                }
                box(input_pdb_path=f'{protein_name}_filtered_pockets/{pocket}_vert.pqr',output_pdb_path=f'{protein_name}_filtered_pockets/{pocket}_box.pdb',properties=prop)

              pocket_file = f'{protein_name}_filtered_pockets/{pocket}_box.pdb'
              output_file = os.path.join(args.outdir, f"{lig_name}_{pocket}_{args.algorithm}.pdbqt")
              args.output_file = output_file
              if args.algorithm == 'Vina':
                args.protein = rec_pdbqt
                args.ligand = lig_pdbqt
                result = vina_dock(args, pocket_file, ligand_file)
                docking_results.append((pocket, result.stdout))
              elif args.algorithm == 'Smina':
                result = smina_dock(args, pocket_file, ligand_file)
                docking_results.append((pocket, result.stdout))

              for log_file in glob.glob("log*.out"):
                os.remove(log_file)
              for log_file in glob.glob("log*.err"):
                os.remove(log_file)
        else:
          output_file = os.path.join(args.outdir, f"{lig_name}_{args.algorithm}.pdbqt")
          args.output_file = output_file
          if args.algorithm == 'Vina':
              args.protein = rec_pdbqt
              args.ligand = lig_pdbqt
              result = vina_dock(args, '', ligand_file)
              docking_results.append(('blind_dock:', result.stdout))
          elif args.algorithm == 'Smina':
              result = smina_dock(args, '', ligand_file)
              docking_results.append(('blind_dock:', result.stdout))




    # if args.algorithm == 'Vina' and lig_ext != 'pdbqt':
    #     convert_ligand_to_pdbqt(ligand_file, lig_pdbqt, lig_ext)

    # if not args.blind:
    #     docking_results = []
    #     run_fpocket_hunting(protein_file, protein_name, args)
    #     pockets = run_fpocket_filtering(protein_name)
    #     for pocket in tqdm(pockets, desc=f"Docking {protein_name} and {lig_name}"):
    #       with Capturing() as output3:
    #         fpocket_select(f'{protein_name}_filtered_pockets.zip', f'{protein_name}_filtered_pockets/{pocket}_atm.pdb', f'{protein_name}_filtered_pockets/{pocket}_vert.pqr')
    #         prop = {
    #             'offset': 2,
    #             'box_coordinates': True
    #         }
    #         box(input_pdb_path=f'{protein_name}_filtered_pockets/{pocket}_vert.pqr',output_pdb_path=f'{protein_name}_filtered_pockets/{pocket}_box.pdb',properties=prop)

    #       pocket_file = f'{protein_name}_filtered_pockets/{pocket}_box.pdb'
    #       output_file = os.path.join(args.outdir, f"{lig_name}_{pocket}_{args.algorithm}.pdbqt")
    #       args.output_file = output_file
    #       if args.algorithm == 'Vina':
    #         args.protein = rec_pdbqt
    #         args.ligand = lig_pdbqt
    #         result = vina_dock(args, pocket_file, ligand_file)
    #         docking_results.append((pocket, result.stdout))
    #       elif args.algorithm == 'Smina':
    #         result = smina_dock(args, pocket_file, ligand_file)
    #         docking_results.append((pocket, result.stdout))

    #       for log_file in glob.glob("log*.out"):
    #         os.remove(log_file)
    #       for log_file in glob.glob("log*.err"):
    #         os.remove(log_file)
    # else:
    #     docking_results = []
    #     output_file = os.path.join(args.outdir, f"{lig_name}_{args.algorithm}.pdbqt")
    #     args.output_file = output_file
    #     if args.algorithm == 'Vina':
    #         args.protein = rec_pdbqt
    #         args.ligand = lig_pdbqt
    #         result = vina_dock(args, '', ligand_file)
    #         docking_results.append(('blind_dock:', result.stdout))
    #     elif args.algorithm == 'Smina':
    #         result = smina_dock(args, '', ligand_file)
    #         docking_results.append(('blind_dock:', result.stdout))

    return docking_results

def write_docking_results_to_file(docking_results, args, protein_name, algorithm):
    with open(os.path.join(args.outdir, f'{args.name}_{algorithm}.out'), 'w+') as out:
        for num, res in docking_results:
            res_out = res.decode('utf-8')
            out.write(f'{protein_name}_{num}: \n')
            out.write(res_out)
            out.write('\n')
            out.write('-------------------------------------------------------------------------------------------------- \n')

def run_fpocket_hunting(protein_file, protein_name, args):
    prop = {
        'min_radius': args.min_radius,
        'max_radius': args.max_radius,
        'num_spheres': args.min_alpha_spheres,
        'sort_by': 'score'
    }
    print('Pocket hunting...')
    with Capturing() as output1:
        fpocket_run(input_pdb_path=protein_file,
                    output_pockets_zip=f'{protein_name}_raw_pockets.zip',
                    output_summary=f'{protein_name}_fpocket_info.json',
                    properties=prop)
    return output1

def run_fpocket_filtering(protein_name):
    prop = {'score': [0.2, 1]}
    print('Pocket filtering...')
    with Capturing() as output2:
        fpocket_filter(f'{protein_name}_raw_pockets.zip', f'{protein_name}_fpocket_info.json', f'{protein_name}_filtered_pockets.zip', properties=prop)
    pockets = parse_pocket_output(output2)
    shutil.unpack_archive(f'{protein_name}_filtered_pockets.zip', f'{protein_name}_filtered_pockets', 'zip')
    return pockets

# def perform_docking(args):
    protein_file = os.path.abspath(args.protein)
    ligand_file = os.path.abspath(args.ligand)
    if args.algorithm == 'LightDock':
      lightdock_setup(args)
      lightdock_run(f'{args.outdir}/setup.json', args.sim_steps, args.outdir)
      generate_ant_thony_list(args)
      rank_lightdock(args)
      return 
    else:
      protein_name = os.path.basename(protein_file).split('.')[0]
      prot_ext = os.path.basename(protein_file).split('.')[1]
      lig_name = os.path.basename(ligand_file).split('.')[0]
      lig_ext = os.path.basename(ligand_file).split('.')[1]
      rec_pdbqt = f'{os.path.join(args.outdir, protein_name)}.pdbqt'
      lig_pdb = f'{os.path.join(args.outdir, lig_name)}.pdb'
      lig_pdbqt = f'{os.path.join(args.outdir, lig_name)}.pdbqt'
      if prot_ext != 'pdbqt' and args.algorithm != 'Smina':
        print(f'Converting {protein_file} to {rec_pdbqt}...')
        convert_rec = f'obabel -ipdb {protein_file} --partialcharge eem -h -xr -opdbqt -O {rec_pdbqt}'.split(' ')
        subprocess.run(convert_rec, stdout=subprocess.DEVNULL)
      if args.algorithm == 'Vina' and os.path.basename(ligand_file).split('.')[1] != 'pdbqt':
        if lig_ext == "pdb":
          convert_lig = f'obabel -ipdb {lig_pdb} -xr -h --partialcharge eem -opdbqt -O {lig_pdbqt}'.split(' ')
        else:
          convert_lig = f'obabel -i{lig_ext} {ligand_file} -h --partialcharge eem -opdbqt -O {lig_pdbqt}'.split(' ')
        print(f'Converting {ligand_file} to {lig_pdbqt}...')
        subprocess.run(convert_lig, stdout=subprocess.DEVNULL)
      
      if not args.blind:
        prop = {
        'min_radius': args.min_radius,
        'max_radius': args.max_radius,
        'num_spheres': args.min_alpha_spheres,
        'sort_by': 'score'
        }
        print('Pocket hunting...')
        with Capturing() as output1:
          fpocket_run(input_pdb_path=protein_file,
              output_pockets_zip=f'{protein_name}_raw_pockets.zip',
              output_summary=f'{protein_name}_fpocket_info.json',
              properties=prop)

        prop = {
        'score': [0.2, 1],
        }
        print('Pocket filtering...')
        with Capturing() as output2:
          fpocket_filter(f'{protein_name}_raw_pockets.zip', f'{protein_name}_fpocket_info.json', f'{protein_name}_filtered_pockets.zip', properties = prop)
        pockets = parse_pocket_output(output2)
        shutil.unpack_archive(f'{protein_name}_filtered_pockets.zip', f'{protein_name}_filtered_pockets', 'zip')
        docking_results = []
        for pocket in tqdm(pockets, desc="Docking..."):
          with Capturing() as output3:
            fpocket_select(f'{protein_name}_filtered_pockets.zip', f'{protein_name}_filtered_pockets/{pocket}_atm.pdb', f'{protein_name}_filtered_pockets/{pocket}_vert.pqr')
            prop = {
                'offset': 2,
                'box_coordinates': True
            }
            box(input_pdb_path=f'{protein_name}_filtered_pockets/{pocket}_vert.pqr',output_pdb_path=f'{protein_name}_filtered_pockets/{pocket}_box.pdb',properties=prop)



          pocket_file = f'{protein_name}_filtered_pockets/{pocket}_box.pdb'
          output_file = os.path.join(args.outdir, f"{lig_name}_{pocket}_{args.algorithm}.pdbqt")
          args.output_file = output_file
          if args.algorithm == 'Vina':
            args.protein = rec_pdbqt
            args.ligand = lig_pdbqt
            result = vina_dock(args, pocket_file)
            docking_results.append((pocket, result.stdout))
          elif args.algorithm == 'Smina':
            result = smina_dock(args, pocket_file)
            docking_results.append((pocket, result.stdout))

          for log_file in glob.glob("log*.out"):
            os.remove(log_file)
          for log_file in glob.glob("log*.err"):
            os.remove(log_file)
      else:
        docking_results = []
        output_file = os.path.join(args.outdir, f"{lig_name}_{args.algorithm}.pdbqt")
        args.output_file = output_file
        if args.algorithm == 'Vina':
            args.protein = rec_pdbqt
            args.ligand = lig_pdbqt
            result = vina_dock(args, '')
            docking_results.append(('blind_dock:', result.stdout))
        elif args.algorithm == 'Smina':
            result = smina_dock(args, '')
            docking_results.append(('blind_dock:', result.stdout))

    
    return docking_results

def extract_box_info_from_pdb(pdb_file_path):
    with open(pdb_file_path, 'r') as f:
        lines = f.readlines()
    
    for line in lines:
        if "REMARK BOX CENTER" in line:
            parts = line.split(":")
            center = tuple(map(float, parts[1].split("SIZE")[0].strip().split()))
            size = tuple(map(float, parts[2].strip().split()))
            return center, size
        
def smina_dock(args, pocket_file, ligand_file):
    # print(f"Smina docking with {args.protein} and {ligand_file} in pocket {pocket_file}")
    if args.blind:
      print('Smina blind docking...')


      # Prepare the Smina command
      smina_cmd = [
          "smina",
          "-r", args.protein,
          "-l", args.ligand,
          "--autobox_ligand", args.ligand,
          "--exhaustiveness", str(args.exhaustiveness),
          "-o", args.output_file
      ]
    else:
      # Extract center and size from the pocket PDB file
      center, size = extract_box_info_from_pdb(pocket_file)
      # Prepare the Smina command
      smina_cmd = [
          "smina",
          "-r", args.protein,
          "-l", args.ligand,
          "--center_x", str(center[0]),
          "--center_y", str(center[1]),
          "--center_z", str(center[2]),
          "--size_x", str(size[0]),
          "--size_y", str(size[1]),
          "--size_z", str(size[2]),
          "--minimize",
          "--exhaustiveness", str(args.exhaustiveness),
          "-o", args.output_file
      ]

    # Run the Smina command
    result = subprocess.run(smina_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not args.ligand.endswith('.txt'):
      print(f"Docking completed. Results saved to {args.output_file}")
    return result

def vina_dock(args, pocket_file, ligand_file):
    # print(f"Vina docking with {args.protein} and {ligand_file} in pocket {pocket_file}")

    # Extract center and size from the pocket PDB file
    center, size = extract_box_info_from_pdb(pocket_file)

    # Prepare the Vina command
    vina_cmd = [
        "vina",
        "--receptor", args.protein,
        "--ligand", args.ligand,
        "--center_x", str(center[0]),
        "--center_y", str(center[1]),
        "--center_z", str(center[2]),
        "--size_x", str(size[0]),
        "--size_y", str(size[1]),
        "--size_z", str(size[2]),
        "--exhaustiveness", str(args.exhaustiveness),
        "--out", args.output_file,    ]

    # Run the Vina command
    result = subprocess.run(vina_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if not args.ligand.endswith('.txt'):
      print(f"Docking completed. Results saved to {args.output_file}")
    return result

def calculate_rmsd(chain1, chain2):
    atoms1 = [atom for atom in chain1.get_atoms()]
    atoms2 = [atom for atom in chain2.get_atoms()]
    
    # Make sure the two lists have the same size
    if len(atoms1) != len(atoms2):
        return float('inf')
        
    sup = Superimposer()
    sup.set_atoms(atoms1, atoms2)
    return sup.rms


def find_best_match(structure1, structure2):
    min_rmsd = float('inf')
    best_match = (None, None)
    
    for chain1 in structure1.get_chains():
        for chain2 in structure2.get_chains():
            rmsd = calculate_rmsd(chain1, chain2)
            
            if rmsd == float('inf'):
                continue
            
            if rmsd < min_rmsd:
                min_rmsd = rmsd
                best_match = (chain1, chain2)
                
    return best_match

def fixer_of_pdbs(args):
    fixed_pdb_files = []
    if args.just_relax:
      alterations = {}
      fixed_pdb = fix_pdb(args.receptor, alterations, args)
      fixed_pdb_files.append(fixed_pdb)
    else:
      receptor = fix_pdb(args.receptor, alterations={}, args=args)
      ligand = fix_pdb(args.ligand, alterations={}, args=args)
      fixed_pdb_files.append(receptor)
      fixed_pdb_files.append(ligand)
    # elif args.structure:
    #     alterations = {}
    #     fixed_pdb = fix_pdb(args.structure, alterations, args)
    #     fixed_pdb_files.append(fixed_pdb)
    # elif args.dir:
    #     pdb_files = [f for f in os.listdir(args.dir) if f.endswith('.pdb')]
    #     with Pool(int(args.n_workers)) as p:
    #         fixed_pdb_files = list(tqdm(p.imap_unordered(partial(process_pdb, args), pdb_files), total=len(pdb_files), desc="Preprocessing PDBs..."))

    return fixed_pdb_files

def process_pdb(args, filename):
    alterations = {}
    fixed_pdb = fix_pdb(os.path.join(args.dir, filename), alterations, args)
    return fixed_pdb

def fix_pdb(pdb, alterations, args):
  """Apply pdbfixer to the contents of a PDB file; return a PDB string result.

  1) Replaces nonstandard residues.
  2) Removes heterogens (non protein residues) including water.
  3) Adds missing residues and missing atoms within existing residues.
  4) Adds hydrogens assuming pH=7.0.
  5) KeepIds is currently true, so the fixer must keep the existing chain and
     residue identifiers. This will fail for some files in wider PDB that have
     invalid IDs.

  Args:
    pdbfile: Input PDB file handle.

  Returns:
    A PDB string representing the fixed structure.
  """
  fixer = pdbfixer.PDBFixer(pdb)
  fixer.findNonstandardResidues()
  alterations['nonstandard_residues'] = fixer.nonstandardResidues
  fixer.replaceNonstandardResidues()
  _remove_heterogens(fixer, alterations, keep_water=False)
  fixer.findMissingResidues()
  alterations['missing_residues'] = fixer.missingResidues
  fixer.findMissingAtoms()
  alterations['missing_heavy_atoms'] = fixer.missingAtoms
  alterations['missing_terminals'] = fixer.missingTerminals
  fixer.addMissingAtoms(seed=0)
  fixer.addMissingHydrogens()
  filename = os.path.splitext(os.path.basename(pdb))[0]
  out_file_path = os.path.join(args.outdir, f"{filename}_fixed.pdb")

  with open(out_file_path, 'w+') as f:
    PDBFile.writeFile(fixer.topology, fixer.positions, f, keepIds=True)

  return out_file_path


def clean_structure(pdb_structure, alterations_info):
  """Applies additional fixes to an OpenMM structure, to handle edge cases.

  Args:
    pdb_structure: An OpenMM structure to modify and fix.
    alterations_info: A dict that will store details of changes made.
  """
  _replace_met_se(pdb_structure, alterations_info)
  _remove_chains_of_length_one(pdb_structure, alterations_info)


def _remove_heterogens(fixer, alterations_info, keep_water):
  """Removes the residues that Pdbfixer considers to be heterogens.

  Args:
    fixer: A Pdbfixer instance.
    alterations_info: A dict that will store details of changes made.
    keep_water: If True, water (HOH) is not considered to be a heterogen.
  """
  initial_resnames = set()
  for chain in fixer.topology.chains():
    for residue in chain.residues():
      initial_resnames.add(residue.name)
  fixer.removeHeterogens(keepWater=keep_water)
  final_resnames = set()
  for chain in fixer.topology.chains():
    for residue in chain.residues():
      final_resnames.add(residue.name)
  alterations_info['removed_heterogens'] = (
      initial_resnames.difference(final_resnames))


def _replace_met_se(pdb_structure, alterations_info):
  """Replace the Se in any MET residues that were not marked as modified."""
  modified_met_residues = []
  for res in pdb_structure.iter_residues():
    name = res.get_name_with_spaces().strip()
    if name == 'MET':
      s_atom = res.get_atom('SD')
      if s_atom.element_symbol == 'Se':
        s_atom.element_symbol = 'S'
        s_atom.element = element.get_by_symbol('S')
        modified_met_residues.append(s_atom.residue_number)
  alterations_info['Se_in_MET'] = modified_met_residues


def _remove_chains_of_length_one(pdb_structure, alterations_info):
  """Removes chains that correspond to a single amino acid.

  A single amino acid in a chain is both N and C terminus. There is no force
  template for this case.

  Args:
    pdb_structure: An OpenMM pdb_structure to modify and fix.
    alterations_info: A dict that will store details of changes made.
  """
  removed_chains = {}
  for model in pdb_structure.iter_models():
    valid_chains = [c for c in model.iter_chains() if len(c) > 1]
    invalid_chain_ids = [c.chain_id for c in model.iter_chains() if len(c) <= 1]
    model.chains = valid_chains
    for chain_id in invalid_chain_ids:
      model.chains_by_id.pop(chain_id)
    removed_chains[model.number] = invalid_chain_ids
  alterations_info['removed_chains'] = removed_chains


def get_structure(file_path):
    parser = PDBParser()
    return parser.get_structure(file_path.split('/')[-1].split('.')[0], file_path)

def find_matching_chain(structure1, structure2):
    for chain1 in structure1.get_chains():
        for chain2 in structure2.get_chains():
            atoms1 = [atom.id for atom in chain1.get_atoms()]
            atoms2 = [atom.id for atom in chain2.get_atoms()]
            if set(atoms1) == set(atoms2):
                return chain1, chain2
    return None, None

def superimpose_chains(chain1, chain2):
    sup = Superimposer()
    atoms1 = [atom for atom in chain1.get_atoms()]
    atoms2 = [atom for atom in chain2.get_atoms()]
    sup.set_atoms(atoms1, atoms2)
    sup.apply(chain1.get_atoms())

def save_complex(receptor_structure, ligand_structure, output_file):
    io = PDBIO()
    io.set_structure(receptor_structure)
    io.save(output_file)
    io.set_structure(ligand_structure)
    io.save(output_file, write_end=1, append_end=1)

def lightdock(args, ligands):
    master_outdir = os.path.abspath(args.outdir) # Save the original output directory
    protein_file = os.path.basename(args.protein)
    args.protein = os.path.abspath(args.protein)
    if len(ligands) > 1:
        for ligand_path in ligands:
            ligand_name = os.path.splitext(os.path.basename(ligand_path))[0]
            args.ligand = os.path.abspath(ligand_path)  # Make sure it's an absolute path
            args.outdir = os.path.join(master_outdir, ligand_name)  # Create a sub-directory for each ligand

            # Create the sub-directory if it doesn't exist
            if not os.path.exists(args.outdir):
                os.makedirs(args.outdir)

            prot_dest = os.path.join(args.outdir, os.path.basename(args.protein))
            lig_dest = os.path.join(args.outdir, os.path.basename(args.ligand))
            if os.path.abspath(args.protein) != os.path.abspath(prot_dest):
              shutil.copy(args.protein, args.outdir)
            if os.path.abspath(args.ligand) != os.path.abspath(lig_dest):
              shutil.copy(args.ligand, args.outdir) 

            args.protein = os.path.join(args.outdir, protein_file)     
            args.ligand = os.path.join(args.outdir, os.path.basename(args.ligand))  
            # Run the LightDock pipeline for this ligand
            lightdock_setup(args)
            lightdock_run(os.path.join(args.outdir, 'setup.json'), args.sim_steps, args.outdir, args.n_workers)
            generate_ant_thony_list(args)
            rank_lightdock(args)
            
    else:  # If there's only one ligand, proceed as usual
        ligand_file = os.path.basename(args.ligand)
        prot_dest = os.path.join(args.outdir, os.path.basename(args.protein))
        lig_dest = os.path.join(args.outdir, os.path.basename(args.ligand))
        # Check if source and destination are the same
        if os.path.abspath(args.protein) != os.path.abspath(prot_dest):
            shutil.copy(args.protein, args.outdir)
        if os.path.abspath(args.ligand) != os.path.abspath(lig_dest):
            shutil.copy(args.ligand, args.outdir)  

        args.protein = os.path.join(args.outdir, protein_file)
        args.ligand = os.path.join(args.outdir, ligand_file)  # Make sure it's an absolute path
        lightdock_setup(args)
        lightdock_run(os.path.join(args.outdir, 'setup.json'), args.sim_steps, args.outdir, args.n_workers)
        generate_ant_thony_list(args)
        rank_lightdock(args)

def lightdock_setup(args):
    if args.restraints:
      cmd = ["lightdock3_setup.py", args.protein, args.ligand, "--outdir", args.outdir, "--noxt", "--noh", "--now", "-s", str(args.swarms), "--seed_points", str(args.RNG_seed), "--seed_anm", str(args.RNG_seed), "--rst", args.restraints]
    else:
      print(args.protein)
      print(args.ligand)
      cmd = ["lightdock3_setup.py", args.protein, args.ligand,"--outdir", args.outdir, "--noxt", "--noh", "--now", "-s", str(args.swarms), "--seed_points", str(args.RNG_seed), "--seed_anm", str(args.RNG_seed)]
    subprocess.run(cmd)

def lightdock_run(setup_json, steps, outdir, cpus):
    cmd = ["lightdock3.py", setup_json, str(steps), '--outdir', outdir, '-c', str(cpus)]
    subprocess.run(cmd)

def generate_ant_thony_list(args):
    with open(f"{args.outdir}/generate_lightdock.list", "w") as f:
        for i in range(args.swarms):
            f.write(f"cd {args.outdir}/swarm_{i}; lgd_generate_conformations.py {args.protein} {args.ligand} gso_{args.sim_steps}.out {args.sim_steps} > /dev/null 2> /dev/null;\n")

    with open(f"{args.outdir}/cluster_lightdock.list", "w") as f:
        for i in range(args.swarms):
            f.write(f"cd {args.outdir}/swarm_{i}; lgd_cluster_bsas.py gso_{args.sim_steps}.out > /dev/null 2> /dev/null;\n")

    cmd = ["ant_thony.py", "-c", str(args.n_workers), f"{args.outdir}/generate_lightdock.list"]
    subprocess.run(cmd)

    cmd = ["ant_thony.py", "-c", str(args.n_workers), f"{args.outdir}/cluster_lightdock.list"]
    subprocess.run(cmd)

def rank_lightdock(args):
    cmd = ["lgd_rank.py", str(args.swarms), str(args.sim_steps), "--outdir", args.outdir]
    subprocess.run(cmd)
