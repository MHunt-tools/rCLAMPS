# Optimizes a log-linear model for dependency between amino acids occupying 
# match state positions in an HMM for a DNA-binding protein family and nucleotide 
# bases occupying the key contact positions in a set of corresponidng bound DNA sequences. 
# Does so by earching the set of latent parameters corresponding to PWM offsets and orientations 
# that maximize likelihood of the ll-model.  Since the problem is generally intractible,
# an approximately optimal model is found using MCMC (Gibbs sampling) to alternate between 
# estimating the optimal ll-model paramters with a PWM-protein pair held out and sampling a 
# new offset and orientation for the held out pair, given the current optimal model, cycling
# through all pairs.  This implementation allows for block sampling for similar proteins.

from runhmmer import *
from matAlignLib import *
from pwm import makeNucMatFile, makeLogo, rescalePWM
from copy import deepcopy
import numpy as np
import multiprocessing
from getHomeoboxConstructs import getUniprobePWMs, getFlyFactorPWMs
from getHomeoboxConstructs import parseNoyes08Table, makeMatchStateTab
from getHomeoboxConstructs import readFromFasta, writeToFasta, subsetDict
from moreExp_veri import getPWM, getPWM_barrera
import time
import pickle
import argparse
import scipy.stats
import sklearn.metrics
import math
from decimal import Decimal
from scipy.stats import dirichlet
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import mean_squared_error
from scipy import sparse
from scipy.stats import multinomial

OUTPUT_DIRECTORY = './my_results/allHomeodomainProts/'  # Set to any output directory you want
ORIGINAL_INPUT_FORMAT = True         # Set to True for exact reproducion of manuscript models
                                     # Set to False to give inputs in format from ../precomputedInputs/ 
RUN_GIBBS = True                     # Set to False if only want to troubleshoot prior to running Gibbs sampler
HMMER_HOME = '/home/jlwetzel/src/hmmer-3.3.1/src/'
EXCLUDE_TEST = False   # True if want to exlude 1/2 of Chu proteins for testing
MWID = 6               # Number of base positions in the contact map; set for backward compatibility
RAND_SEED = 382738375  # Numpy random seed for used for manuscript results
MAXITER = 15           # Maximum number of iterations per Markov chain
N_CHAINS = 100         # Number of Markov chains to use
INIT_ORACLE = False    # Deprecated ... was used to compare to previous Naive Bayes implementation
SAMPLE = 100           # Integer to multiply PWM columns by when converting to counts
OBS_GRPS = 'grpIDcore' # Perform group updates based on identical DNA-contacting protein residues
if EXCLUDE_TEST:
    SEED_FILE = '../precomputedInputs/fixedStarts_homeodomains_noTest.txt' # Initial seeds based on structures
else:
    SEED_FILE = '../precomputedInputs/fixedStarts_homeodomains_all.txt'    # Initial seeds based on structures

if ORIGINAL_INPUT_FORMAT:
    # Set parameters for adjusting the model topology
    # (i.e., the binarized contact matrix)
    CORE_POS = 'useStructInfo'    # Uses the precomputed structural alignments
    APOS_CUT = 'cutAApos_1.0_0.05'  # Controls AA contact threshold for binarization
    EDGE_CUT = 'edgeCut_1.0_0.05'   # Controls AA contact threshold for binarization
    MAX_EDGES_PER_BASE = None       # None means to just ignore this parameter
    RESCALE_PWMS = True             # True if using PWM rescaling
else:
    #################
    # Note:  This input format is not yet fully tested, but should work (see files listed here for format)
    PROT_SEQ_FILE = '../precomputedInputs/proteins_homeodomains_hasPWM.fa'  # Input protein sequence fasta file
    PWM_INPUT_TABLE = '../precomputedInputs/pwmTab_homeodomains_all.txt'   # A table of PWMs corresponding to prots in PROT_SEQ_FILE
    CONTACT_MAP = '../precomputedInputs/homeodomain_contactMap.txt'  # A contact map for the domain family
    HMM_FILE = '../pfamHMMs/Homeobox.hmm'    # Location of hmm file
    HMM_LEN = 57                             # Number of match states in HMM file
    HMM_NAME = 'Homeobox'                    # A name for the HMM
    HMM_OFFSET = 2                           # Used to offset HMM states to canonical numbering scheme for Homoedomains
                                             # Set to zero to use default HMM match state numbers (0-indexed)
    TEST_PROT_FILE = '../precomputedInputs/testProts_chu2012_randSplit.txt'  # protein/PWM pairs from PROT_SEQ_FILE that are reserved for later testing    
    #################

# Used by various functions - do not change these
BASE = ['A','C','G','T']
REV_COMPL = {'A':'T','C':'G','G':'C','T':'A'}
AMINO = ['A','C','D','E','F','G','H','I','K','L',
         'M','N','P','Q','R','S','T','V','W','Y']
B2IND = {x: i for i, x in enumerate(BASE)}
A2IND = {x: i for i, x in enumerate(AMINO)}
IND2B = {i: x for i, x in enumerate(BASE)}
IND2A = {i: x for i, x in enumerate(AMINO)}
COMPL_BASE = {0:3, 1:2, 2:1, 3:0}

# Pass arguments from command line
parser = argparse.ArgumentParser()
parser.add_argument("-i", "--iteration",
    type=int, action="store", default=25)
parser.add_argument("-c", "--nchains",
    type=int, action="store", default=4)
parser.add_argument("--initoracle",
    action="store_true", default=False)
parser.add_argument("-s", "--sample",
    type=int, action="store", default=6)
args = parser.parse_args()

### This global is no longer necessary or used due to use of example structural
### alignment seeding.  However, it still exists in many function signatures ...
ORIENT = {'b08': {'Pitx1': 1}, 'cisbp': {'x': 1}}   # Reference start orientation for one of the structures

###############################################################################
# older functions:  Many still used in GLM implentation.  Some are deprecated #
###############################################################################

def readPDBcoords(fname, start = 0, stop = None, zeroInd = False,
                  skipRNA = False, mType = 'nuc',excludeBackbone = True):
    # Returns a dictionary of per-chain position atoms and coords
    # for a pdb polymer files, ignoring hydrogen atoms.
    # Returns None for RNA files if skipRNA = True

    # For backbone exclusion
    if mType == 'nuc':
        bb = set(["P","OP1","OP2","C5'","C4'","C3'",
                  "C2'","C1'","O5'","O4'","O3'","O2'"])
        res2code = {'A': 'A', 'C': 'C', 'G':'G', 'T':'T'}
    elif mType == 'peptide':
        bb = set(["C","N","O"])
        res2code = {
            'ALA':'A','ARG':'R','ASN':'N','ASP':'D','CYS':'C',
            'GLU':'E','GLN':'Q','GLY':'G','HIS':'H','ILE':'I',
            'LEU':'L','LYS':'K','MET':'M','PHE':'F','PRO':'P',
            'SER':'S','THR':'T','TRP':'W','TYR':'Y','VAL':'V'}

    bPos = {}
    try:
        fin = open(fname, 'r')
    except IOError:
        return None
    #print fname
    for i, line in enumerate(fin):

        # Are we at the end of the file?
        if line == 'TER' or line == 'TER\n':
            break

        atomName, res, pos, atom = \
            line[12:16].strip(), line[17:20].strip(), \
            int(line[22:26]), line[76:].strip()
        x, y, z = float(line[30:38]), float(line[38:46]), float(line[46:54])

        if atomName in bb:
            atomLoc = 'bb'
        else:
            atomLoc = 'non-bb'

        # Ignore hydrogens and/or backbone atoms
        if atom == 'H':     # Ignore hydrogen atoms
            continue
        if excludeBackbone and atomLoc == 'bb':
            continue

        # Convert to standard DNA code for nucleotides
        if mType == 'nuc' and skipRNA and len(res) == 1: # Ribonucleotide found
            fin.close()
            print "Encountered non-deoxy base ... skip for now"
            return None
        elif mType == 'nuc' and res[-1] == 'M': #Methylated base
            res = res[1]
        elif mType == 'nuc' and res[1:] == '2S': #??
            res = res[0]
        elif mType == 'nuc' and res == 'DU':  #Deoxyuracil (de-aminated cytosine)
            res = 'C'
        elif mType == 'nuc':
            res = res[-1]

        # Add information to our list
        if not bPos.has_key(pos):
            bPos[pos] = {}
            bPos[pos]['res'] = res
            bPos[pos]['atoms'] = [atom]
            bPos[pos]['atomNames'] = [atomName]
            bPos[pos]['atomLocs'] = [atomLoc]
            bPos[pos]['coords'] = [(x,y,z)]
            bPos[pos]['chainID'] = fname.split('/')[-1].strip().split('.')[0]
        else:
            bPos[pos]['atoms'].append(atom)
            bPos[pos]['atomNames'].append(atomName)
            bPos[pos]['atomLocs'].append(atomLoc)
            bPos[pos]['coords'].append((x,y,z))
    fin.close()

    # Use zero indexing if desired
    if zeroInd:
        bPos2 = {}
        for i, pos in enumerate(sorted(bPos.keys())):
            bPos2[i] = bPos[pos]
            del bPos[pos]
        bPos = bPos2

    # Remove positions we're not interested in
    if stop is not None:
        for pos in bPos.keys():
            if pos < start or pos > stop:
                del bPos[pos]

    return bPos


def makePWMtab(pwms, fname):
    """
    Output function. Writing pwms in the output.
    """
    fout = open(fname, 'w')
    fout.write('prot\tbpos\tbase\tprob\n')
    for k in pwms.keys():
        m = pwms[k]
        for i in range(len(m)):
            for j in range(len(m[i])):
                fout.write('%s\t%d\t%s\t%e\n' %(k,i,IND2B[j],m[i,j]))
    fout.close()

def readPWMtab(fname):
    # Reads in a PWM table in the format output by makePWMtab
    fin = open(fname, 'r')
    fin.readline()
    pwms = {}
    for line in fin:
        prot, bpos, base, freq = line.strip().split('\t')
        bpos = int(bpos)
        freq = float(freq)
        if prot not in pwms:
            pwms[prot] = [[0.0]*4]
        elif bpos == len(pwms[prot]):
            pwms[prot].append([0.0]*4)
        pwms[prot][bpos][B2IND[base]] = freq
    fin.close()
    for p in pwms.keys():
        pwms[p] = np.array(pwms[p])
    return pwms

def getAlignedPWMs(pwms, aSeqs, start, mWid, flipAli = False):
    """
    Output function.
    Returns a new set of PWMs, truncated on each side to the
    aligned region
    """
    npwms = {}
    for p in pwms.keys():
        pwm = pwms[p]
        npwm = np.zeros((mWid,4), dtype = 'float')
        s = start[p]
        for i in range(mWid):
            npwm[i,:] = pwm[i+s,:]
        if flipAli:
            npwm = matrix_compl(npwm)
        npwms[p] = npwm
    return npwms

def makeAllLogos(pwms, aSeqs, logoDir, keysToUse = None):
    """
    Output function
    Place logos for every pwm and ouput to logoDir
    """

    if keysToUse == None:
        keysToUse = sorted(pwms.keys())

    if not os.path.exists(logoDir):
        os.makedirs(logoDir)

    i = 0
    for k in keysToUse:
        pwm, core = pwms[k], aSeqs[k]
        logoLab = '_'.join([k, core, str(i)])
        makeNucMatFile('./tmp/','tmp',pwm)
        makeLogo('./tmp/tmp.txt',logoDir+logoLab+'.pdf',
                 alpha = 'dna', colScheme = 'classic')
        os.system('rm %s' %'./tmp/tmp.txt')
        i += 1

def getOrientedPWMs(pwms, rev, reorient = 1):
    """
    Algorithm function.
    Returns PWMs according to rev
    """

    # Orient pwms according to rev
    pwms_o = {}
    for k in pwms.keys():
        if rev[k] == reorient:
            pwms_o[k] = matrix_compl(pwms[k])
        else:
            pwms_o[k] = deepcopy(pwms[k])
    return pwms_o


def reverseBaseOrient(pwms, mWid = None, start = None, rev = None):
    """ Reverses the orientation of all pwms and optionally also
    corrects starting positions and reverse flags if a motif width
    is provided.
    """
    pwms_o = {k: matrix_compl(pwms[k]) for k in pwms.keys()}
    if mWid is not None:
        opp = {0:1,1:0}
        start_o = {k: len(pwms[k])-start[k]-mWid for k in start.keys()}
        rev_o = {k: opp[rev[k]] for k in rev.keys()}
        return pwms_o, start_o, rev_o
    else:
        return pwms_o


def getContactFractions(fname):
    """
    Input function.
    Returns a nested dictionary containing the weighted
    fraction of observed structures for which each amino acid
    position was observed to contact at least one base.
    """
    fin = open(fname, 'r')
    fin.readline() # strip header
    wts = {'base': {}, 'backbone': {}}
    for line in fin:
        l = line.strip().split('\t')
        apos, cType, w = eval(l[0]), l[1], eval(l[2])
        wts[cType][apos] = w
    fin.close()
    return wts

def getEdgeWtMats(fname):
    """
    Input function.
    Returns a matrix encoding the uniqueness weighted fraction
    of structures for which contact is made between each aa and base
    position (either backbone or base contact )
    """
    fin = open(fname, 'r')
    fin.readline() # strip header
    wts = {'base': {}, 'backbone': {}}
    allApos = set()
    allBpos = set()
    for line in fin:
        l = line.strip().split('\t')
        apos, bpos, w = tuple([eval(x) for x in l[:3]])
        cType = l[-1]
        if not (apos in wts[cType]):
            wts[cType][apos] = {}
        wts[cType][apos][bpos] = w
        allBpos.add(bpos)
        allApos.add(apos)
    fin.close()

    wtMats = {}
    for cType in ['base', 'backbone']:
        wtMats[cType] = \
            np.zeros((len(allApos),len(allBpos)), dtype = 'float')
        for i, apos in enumerate(sorted(allApos)):
            for j, bpos in enumerate(sorted(allBpos)):
                wtMats[cType][i,j] = wts[cType][apos][bpos]
    return wtMats, sorted(allApos), sorted(allBpos)

def getAAposByStructInfo(fname, wtCutBB, wtCutBase):
    """
    Input function
    Determine the amino acid positions to consider based on
    structural considerations.
    - fname specifies a text table of precomputed contact info
    - wtCutBB, and wtCutBase are the minimum weighed fraction of
      co-crystal structures in which an apos must contact DNA
      backbone or base, respectively
    """
    cWt = getContactFractions(fname)
    #print(cWt)
    apos = list(set(cWt['backbone'].keys()) & set(cWt['base'].keys()))
    aaPosList = set([x for x in cWt['backbone'].keys() \
                     if cWt['backbone'][x] >= wtCutBB]) | \
                set([x for x in cWt['base'].keys() \
                     if cWt['base'][x] >= wtCutBase])
    return sorted(list(aaPosList))

def getEdgesByStructInfo(fname, aaPos, maxMwid, wtCutBB, wtCutBase,
                         maxEdgesPerBase = None, N51A_bpos = 2):
    """
    Input function
    Determine the bpos to apos dependency edges based on
    structural considerations.
    - fname specifies a text table of precomputed contact info
    - aaPos is a list (or set) of allowable apos
    - maxMwid is the maximum allowable motif width
    - wtCutBB, and wtCutBase are the minimum weighed fraction of
      co-crystal structures in which an apos x must contact
      bpos y on the backbone or base, respectively
    - N51A_bpos specifies the desired 0-indexed position for the
      adenine that interacts with N51 most strongly according
      (position 3 according to Christensen/Noyes, Cell 2008)
    - if maxEdgesPerBase is not None, it defines the maximum allowable
      dependency edges per base positions, where the edges used are
      prioritized based on frequency the of base-contacting amino
      acid positions (after removing aa-positions and edges based
      on aaPos, wtCutBB, wtCutBase)
    """
    wtMats, rLabs, cLabs = getEdgeWtMats(fname)

    # Make N51 -> Ade contact be position 3
    mStart = np.argmax(wtMats['base'][rLabs.index(51),:]) - N51A_bpos
    edges_hmmPos = {}
    edgeWts = {}
    for bpos in range(maxMwid):
        edges_hmmPos[bpos] = []
        edgeWts[bpos] = []
    for apos in aaPos:
        for bpos in range(maxMwid):
            j = bpos+mStart
            bbWt = wtMats['backbone'][rLabs.index(apos),j]
            baseWt = wtMats['base'][rLabs.index(apos),j]
            if bbWt >= wtCutBB or baseWt >= wtCutBase:
                edges_hmmPos[bpos].append(apos)
                edgeWts[bpos].append(baseWt)

    # Restrict number of edges if desired
    if maxEdgesPerBase is not None:
        for bpos in edges_hmmPos.keys():
            keep = sorted(zip(edgeWts[bpos],edges_hmmPos[bpos]),
                          reverse = True)[:maxEdgesPerBase]
            edges_hmmPos[bpos] = sorted([x[1] for x in keep])

    # Ignores amino acids that do not contact any bases
    aaPosList = set()
    for k in edges_hmmPos.keys():
        aaPosList |= set(edges_hmmPos[k])
    aaPosList = sorted(list(aaPosList))


    # 0-index the edges with non-contacting amino acid positions removed
    edges = {}
    for bpos in range(maxMwid):
        edges[bpos] = []
        for x in edges_hmmPos[bpos]:
            edges[bpos].append(aaPosList.index(x))

    return edges, edges_hmmPos, aaPosList


def getHomeoboxData(dsets, maxMwid, aaPosList = [2,3,5,6,47,50,51,54,55],
                    aposCut = 'cutAApos_X', edgeCut = 'cutEdge_X',
                    maxEdgesPerBase = None, N51A_bpos = 2, 
                    rescalePWMs = False):
    """
    Input function
    Returns a set of amino acids occupying match states and
    mapped to the corresponding PWMs.

    aaPosList is a list of match states in the hmm to consider.
    if this value is set insead to 'useStructInfo', then the list
    is generated based on observed structural contacts
    """
    NOYES08_INFO = '../homeodomain/flyFactorSurvey/noyes_cell08/' + \
    '1-s2.0-S009286740800682X-mmc2_edited.csv'
    NOYES08_PWM_SRC = '../pwms/homeodomain/flyFactorSurvey_20180126.txt'
    BERGER08_FASTA = '../homeodomain/uniprobe_homeodomains_mouse.fa'
    BERGER08_PWM_SRC = '../pwms/homeodomain/uniprobe_20180126_Cell08/'

    CISBP_FASTA = '../cis_bp/prot_mostRecent_noMuts.fa'
    CISBP_PWM_SRC = '../cis_bp/PWM.txt'
    CISBP_SUBSET_FILE = '../cis_bp/motifTable_mostRecent_noMuts.txt'
    CHU_FASTA = '../Chu2012_bsSelections/domains.fasta'
    CHU_PWM_SRC = '../Chu2012_bsSelections/MEME_allMotifs_rw.txt'
    CHU_SUBSET_FILE = '../Chu2012_bsSelections/allMotifs_info.txt'
    BARRERA_FASTA = '../cis_bp/prot_seq_Barrera2016_mutsOnly.fasta'
    BARRERA_PWM_SRC = '../cis_bp/PWM.txt'
    BARRERA_SUBSET_FILE = '../cis_bp/mutInfoTable_Barrera2016_mutsOnly.txt'
    HBOX_HMM = '../pfamHMMs/Homeobox.hmm'
    HBOX_HMM_LEN = 57
    HMM_NAME = 'Homeobox'
    HMM_OFFSET = 2

    # Optionally, get the core positions via structural analysis
    #print aposCut[-1]
    #print [eval(x) for x in aposCut.split('_')[1:]]
    if aaPosList == 'useStructInfo':
        assert aposCut[-1] != 'X'
        cutBB, cutBase = [eval(x) for x in aposCut.split('_')[1:]]
        wtFile = '../structuralAlignmentFiles/'+ \
            'Homeobox_weightedSum_distCut3.6_unionBases.txt'
        aaPosList = getAAposByStructInfo(wtFile, cutBB, cutBase)

    # Optionally, restrict edges based on structural info
    #print edgeCut[-1]
    #print [x for x in edgeCut.split('_')[1:]]
    if edgeCut[-1] != 'X':
        cutBB, cutBase = [eval(x) for x in edgeCut.split('_')[1:]]
        wtFile = '../structuralAlignmentFiles/' + \
            'Homeobox_weightedSum_distCut3.6.txt'
        edges, edges_hmmPos, aaPosList = \
            getEdgesByStructInfo(wtFile, aaPosList, maxMwid, cutBB,
                                 cutBase, maxEdgesPerBase = maxEdgesPerBase,
                                 N51A_bpos = N51A_bpos)
    else:
        # Includes all possible bpos-to-apos edges/dependencies
        edges = {}
        edges_hmmPos = {}
        for i in range(maxMwid):
            edges[i] = []
            edges_hmmPos[i] = []
            for j, apos in enumerate(aaPosList):
                edges[i].append(j)
                edges_hmmPos[i].append(apos)

    # Index relative to first match state of interest
    corePos = [x - HMM_OFFSET for x in aaPosList]

    seqs = {}   # The full length protein construct
    pwms = {}   # The full length pwm
    core = {}   # Concatenated "core" of extended match states for domain HMM
    full = {}   # Full extended match states for domain HMM
    trunc = {}  # Tells whether or not the HMM match was truncated
    for dset in dsets:

        # Get the sequences and PWMs for the particular datasets
        if dset == 'n08':
            seqs[dset] = parseNoyes08Table(NOYES08_INFO)
            pwms[dset] = getFlyFactorPWMs(NOYES08_PWM_SRC,
                                          subset = set(seqs[dset].keys()),
                                          whichPWM = 'Cell', countSmooth = 1)
            fstem = '/'.join(NOYES08_INFO.split('/')[:-1]) + \
                '/ffs_homeodomains_fly'

        elif dset == 'b08':
            seqs[dset] = readFromFasta(BERGER08_FASTA)
            pwms[dset] = getUniprobePWMs(BERGER08_PWM_SRC,
                                         subset = set(seqs[dset].keys()))
            fstem = '/'.join(BERGER08_FASTA.split('/')[:-1]) + \
                '/uniprobe_homeodomains_mouse'

        elif dset == 'cisbp':
            
            # Get the motif IDs of interest
            fin = open(CISBP_SUBSET_FILE, 'r')
            fin.readline()
            motifs = set()
            for line in fin:
                motifs.add(line.strip().split('\t')[5])
            fin.close()

            # Get the TF names of interest
            fin = open(CISBP_FASTA, 'r')
            fin.readline()
            tfnames = set()
            for line in fin:
                if line[0] == '>':
                    tfnames.add(line[1:].strip())
            fin.close()            

            seqs[dset] = readFromFasta(CISBP_FASTA)
            pwms[dset] = getPWM(CISBP_PWM_SRC, tfnames, motifs)
            #print 'PHOX2B' in seqs[dset]
            #print 'PHOX2B' in pwms[dset]
            subsetDict(pwms[dset], set(seqs[dset].keys()))
            fstem = '/'.join(CISBP_FASTA.split('/')[:-1]) + \
                '/homeodomains_cisbp'

        elif dset == 'chu':

            # Read in the motif table of interest
            tfnames, motifs = set(), set()
            fin = open(CHU_SUBSET_FILE,'r')
            for line in fin:
                tfn, mn = line.rstrip().split('\t')
                tfnames.add(tfn)
                motifs.add(mn)
            fin.close()

            seqs[dset] = readFromFasta(CHU_FASTA)
            pwms[dset] = getPWM(CHU_PWM_SRC,tfnames,motifs)
            subsetDict(pwms[dset], set(seqs[dset].keys()))
            fstem = '../Chu2012_bsSelections/allProts_hmmer_out_'

        elif dset == 'barreraMuts':

            # Read in the motif table of interest
            tfnames, motifs, tfnFull = list(), list(), list()
            fin = open(BARRERA_SUBSET_FILE,'r')
            for line in fin:
                l = line.rstrip().split('\t')
                tfn_full, mn = l[0], l[6]
                tfnFull.append(tfn_full)
                tfnames.append(tfn_full.split('_')[0])
                motifs.append(mn)
            fin.close()

            #print tfnames
            #print tfnFull
            seqs[dset] = readFromFasta(BARRERA_FASTA)
            #print tfnames
            #print motifs
            pwms[dset] = getPWM_barrera(BARRERA_PWM_SRC,motifs,tfnFull)
            #print len(seqs[dset].keys())
            #print len(pwms[dset].keys())
            subsetDict(pwms[dset], set(seqs[dset].keys()))
            fstem = '/'.join(BARRERA_FASTA.split('/')[:-1]) + \
                '/homeodomains_barreraMuts'


        # Subset seqeuences to those for which we have PWMs and
        # run hmmer on the set of sequences remaining
        fasta, hmmerout, matchTab = \
            fstem+'_hasPWM.fa', fstem+'_hasPWM.hmmer3.out.txt', \
            fstem+'_hasPWM.matchTab.txt'
        subsetDict(seqs[dset], set(pwms[dset].keys()))
        writeToFasta(seqs[dset], fasta)
        runhmmer3(hmmerout, HBOX_HMM, fasta, HMM_NAME, getdescs(fasta), 
                  hmmerDir = HMMER_HOME)
        core[dset], full[dset], trunc[dset] = \
            makeMatchStateTab(hmmerout, matchTab, seqs[dset],
                              HBOX_HMM_LEN, HMM_NAME, corePos = corePos)
        #print dset, ":"
        #print len(pwms[dset]), len(core[dset]), len(full[dset]), \
        #    len([k for k in trunc[dset].keys() if trunc[dset][k] == True])
        #print [k for k in full[dset].keys() if 'X' in full[dset][k]]
        #print [k for k in trunc[dset].keys() if trunc[dset][k] == True]
        #if dset == 'cisbp':
        #    print 'PHOX2B' in core[dset]
        subsetDict(pwms[dset], set(core[dset].keys()))

        # Rescale the PWMs?
        if rescalePWMs:
            for k in pwms[dset].keys():
                pwms[dset][k] = rescalePWM(pwms[dset][k], maxBaseSelect = 50)

    return seqs, pwms, core, full, trunc, aaPosList, edges, edges_hmmPos

def getProtDistMat(core):
    """
    Input function.
    Create a matrix of hamming distances between protein core seqs
    """
    prots = sorted(core.keys())
    protDist = np.zeros((len(prots),len(prots)), dtype = 'float')
    for i, k1 in enumerate(prots):
        c1 = core[k1]
        for j, k2 in enumerate(prots):
            c2 = core[k2]
            hdist = 0
            for xpos in range(len(c1)):
                if c1[xpos] != c2[xpos]:
                    hdist += 1
            protDist[i,j] = hdist
    return protDist, prots

def assignObsGrps(core, by = 'grpIDcore'):
    """
    Algorithm function.
    Assign observations to groups based on core similarity
    """
    grps = {}
    if by == 'none':
        # Each observation is assigned to its own group
        for k in core.keys():
            grps[k] = [k]
    elif by == 'grpIDcore':
        for k in core.keys():
            if not grps.has_key(core[k]):
                grps[core[k]] = [k]
            else:
                grps[core[k]].append(k)
    elif by == 'hd1':
        distMat, prots = getProtDistMat(core)

    return grps

def initStarts(uniqueProteins, pwms, mWid, fixedStarts = {}):
    """
    Algorithm function.
    Returns initial values for the starting positions and orientations
    of the pwms (random currently)
    """
    start, rev = {}, {}
    for k in uniqueProteins:
        if k not in fixedStarts:
            rev[k] = np.random.randint(low = 0, high = 2)
            start[k] = np.random.randint(low = 0,
                                         high = len(pwms[k])-mWid + 1)
        else:
            rev[k] = fixedStarts[k]['rev']
            start[k] = fixedStarts[k]['start']
    return start, rev

#######################################
# Kaiqian's revised functions for GLM #
#######################################

#### Not currently used ##########
def compute_multinomial_logpmf(x, param):
    n = sum(x)
    #res = float(Decimal(math.factorial(n)).ln())
    res = 0
    for i in range(len(x)):
        res += x[i]*np.log(param[i]) - float(Decimal(math.factorial(x[i])).ln())
    return res
###################################

def useUniqueProteins(obsGrps):
    """
    Function for extracting unique proteins from obsGrps.
    Since several proteins have the same amino acid sequences, they are stored in the obsGrps,
    where obsGrps is a dictionary such as {'EQKTCGNKNQR': ['Six3', 'Six6'], ...}. To improve
    the efficiency of fitting the GLM model, we only consider one protein from same amino acid
    sequence group. This function extracts proteins with different amino acid sequences by simply
    using the first protein in each group.
    :param obsGrps: a dictionary {'amino acid sequence': [an array of proteins]}.
                    e.g.: {'EQKTCGNKNQR': ['Six3', 'Six6'], ...}.
    :return: an array of unique proteins that will be used in fitting the GLM model.
    """
    uniqueProteins = []
    for key in obsGrps.keys():
        uniqueProteins.append(obsGrps[key][0])
    return uniqueProteins

def getTrainPairsAndInfo(rescalePWMs = False, excludeTestSet = True, 
                         addResidues = False):
    # Returns training pwms and core seqs along with info about the
    # model structure for encoding proteins, etc.

    # Read data
    aaPosList = CORE_POS
    seqs, pwms, core, full, trunc, aaPosList, edges, edges_hmmPos = \
        getHomeoboxData(['cisbp', 'chu'], MWID, aaPosList = aaPosList,
                        aposCut = APOS_CUT, edgeCut = EDGE_CUT,
                        maxEdgesPerBase = MAX_EDGES_PER_BASE,
                        N51A_bpos = (MWID-6)/2+2, rescalePWMs = rescalePWMs)

    # Subset the training PWMs to not exclude those with invalid core 
    # sequences or PWMs less than length MWID (as was done in training)
    pwms1, core1, full1 = pwms['cisbp'],core['cisbp'],full['cisbp']
    pwms2, core2, full2 = pwms['chu'], core['chu'], full['chu']


    # Remove examples from cis-bp and chu that correspond to 
    # core seqs in the test set
    testProts = None
    if (excludeTestSet):
        fin = open('../hd1-explore/0_splitChu2012-trainTest/testProts.txt','r')
        testProts = [x.strip() for x in fin.readlines()]
        fin.close()
        for x in [core1, core2, pwms1, pwms2, full1, full2]:
            allProts = x.keys()
            for prot in allProts:
                if prot in testProts:
                    #print prot
                    del x[prot]

    # Combine pwms and core seqs remaining into a single dataset
    pwms, core, full = {}, {}, {}
    for x in [pwms1, pwms2]:
        for prot in x.keys():
            pwms[prot] = x[prot]
    for x in [core1, core2]:
        for prot in x.keys():
            core[prot] = x[prot]
    for x in [full1, full2]:
        for prot in x.keys():
            full[prot] = x[prot]

    if addResidues:
        # If including additional HMM state residues used by Chirstensen et al. 2012
        aaPosList = set()
        for bpos in edges_hmmPos.keys():
            edges_hmmPos[bpos] = sorted(edges_hmmPos[bpos] + [6])
            aaPosList = aaPosList | set(edges_hmmPos[bpos])
        aaPosList = sorted(list(aaPosList))
        for bpos in edges.keys():
            edges[bpos] = [aaPosList.index(x) for x in edges_hmmPos[bpos]]
            for p in full.keys():
                core[p] = ''.join([full[p][x-2] for x in aaPosList])

    trainPWMs, trainCores, trainFull = pwms, core, full
    keep = set([k for k in trainCores.keys() if 'X' not in trainCores[k]])
    [subsetDict(x, keep) for x in [trainPWMs, trainCores,trainFull]]
    keep = set([k for k in trainCores.keys() if '-' not in trainCores[k]])
    [subsetDict(x, keep) for x in [trainPWMs, trainCores,trainFull]]
    keep = set([k for k in trainPWMs.keys() if trainPWMs[k].shape[0] >= MWID])
    [subsetDict(x, keep) for x in [trainPWMs, trainCores,trainFull]]

    return trainPWMs, trainCores, trainFull, edges, edges_hmmPos, aaPosList, testProts

def getTestProtsAndPWMs(testProtNames, rescalePWMs = False, addResidues = False):
    # Returns the the set of test PWMs given the protein names

    # Read data
    aaPosList = CORE_POS
    seqs, pwms, core, full, trunc, aaPosList, edges, edges_hmmPos = \
        getHomeoboxData(['cisbp', 'chu','barreraMuts'], MWID, aaPosList = aaPosList,
                        aposCut = APOS_CUT, edgeCut = EDGE_CUT,
                        maxEdgesPerBase = MAX_EDGES_PER_BASE,
                        N51A_bpos = (MWID-6)/2+2, rescalePWMs = rescalePWMs)

    # Subset the training PWMs to not exclude those with invalid core 
    # sequences or PWMs less than length MWID (as was done in training)
    pwms1, core1, full1 = pwms['cisbp'],core['cisbp'], full['cisbp']
    pwms2, core2, full2 = pwms['chu'], core['chu'], full['chu']
    pwms3, core3, full3 = pwms['barreraMuts'], core['barreraMuts'], full['barreraMuts']

    # Combine pwms and core seqs remaining into a single dataset
    pwms, core, full = {}, {}, {}
    for x in [pwms1, pwms2, pwms3]:
        for prot in x.keys():
            pwms[prot] = x[prot]
    for x in [core1, core2, core3]:
        for prot in x.keys():
            core[prot] = x[prot]  
    for x in [full1, full2, full3]:
        for prot in x.keys():
            full[prot] = x[prot]    

    # Remove pwms not in the test set
    for x in [pwms, core, full]:
        allProts = x.keys()
        for prot in allProts:
            if prot not in testProtNames:
                del x[prot]

    if addResidues:
        # If including additional HMM state residues used by Chirstensen et al. 2012
        aaPosList = set()
        for bpos in edges_hmmPos.keys():
            edges_hmmPos[bpos] = sorted(edges_hmmPos[bpos] + [6])
            aaPosList = aaPosList | set(edges_hmmPos[bpos])
        aaPosList = sorted(list(aaPosList))
        for bpos in edges.keys():
            edges[bpos] = [aaPosList.index(x) for x in edges_hmmPos[bpos]]
            for p in full.keys():
                core[p] = ''.join([full[p][x-2] for x in aaPosList])

    # Remove entries that can't be encoded
    keep = set([k for k in core.keys() if 'X' not in core[k]])
    [subsetDict(x, keep) for x in [pwms, core, full]]
    keep = set([k for k in core.keys() if '-' not in core[k]])
    [subsetDict(x, keep) for x in [pwms, core, full]]
    keep = set([k for k in pwms.keys() if pwms[k].shape[0] >= MWID])
    [subsetDict(x, keep) for x in [pwms, core, full]]   

    return pwms, core, full


###################################

def formGLM_fullX(core, edges, uniqueProteins, obsGrps, numAAs = 19,
                  modelType = 'classifier'):
    """
    Function for forming a full X using all unique proteins including hold out proteins.
    :param core: a dictionary {"protein": "amino acid sequence"}
    :param edges: a dictionary {"dna base position": [amino acid positions]}
    :param uniqueProteins: an array of unique proteins
    :return: a dictionary, each base position j corresponds to a matrix, 
    whose dimension is n*4 by E_j*numAAs, where n is the number of unique proteins,
    and E_j the number of amino acids contact at the jth base position.
    """

    #print(core)
    #print(edges)
    #print(uniqueProteins)

    X = {}
    grpInd = {}  # Maps obsGrp keys to (start, end) matrix row index pairs 
    for j in range(MWID):
        aa_pos = edges[j]
        E_j = len(aa_pos)
        X[j] = np.array([], dtype=np.int64).reshape(0, numAAs*E_j)
        rowNum = 0
        for g in obsGrps.keys():
            grpStart = rowNum
            for p in obsGrps[g]:
                core_seq = core[p]
                x = []
                for i in aa_pos:
                    cur_x = [0] * numAAs
                    aa = A2IND[core_seq[i]]
                    if aa < numAAs:
                        cur_x[aa] = 1
                    x += cur_x
                # For each protein, it has four lines of same X, i.e, four samples for each protein in the logistic regression
                if modelType == 'classifier':
                    x = np.tile(x, (4,1))
                    X[j] = np.concatenate((X[j], x), axis=0)
                    rowNum += 4
                elif modelType == 'regressor':
                    x = np.tile(x, (1,1))
                    X[j] = np.concatenate((X[j], x), axis=0)
                    rowNum += 1                    
            grpEnd = rowNum-1
            grpInd[g] = (grpStart, grpEnd)
    return X, grpInd

def makeCoefTable(model, edges_hmmPos, outfile):
    # Make a table of the model coefficients for exploration
    
    fout = open(outfile, 'w')
    fout.write('bpos\taapos\tbase\taa\tcoef\n')

    print edges_hmmPos
    for j in range(MWID):
        coefs = model[j].coef_
        #print(coefs.shape)
        aa_pos = edges_hmmPos[j]
        bpos = j+1
        for bInd in range(len(coefs)):
            base = IND2B[bInd]
            #print(len(coefs[bInd]))
            for m in range(len(aa_pos)):
                apos = edges_hmmPos[j][m]
                for aaInd in range(19):
                    aa = IND2A[aaInd]
                    val = coefs[bInd][m*19+aaInd]
                    fout.write('%d\tA.%d\t%s\t%s\t%f\n' \
                               %(bpos, apos, base, aa, val))
    fout.close()


def formGLM_trainX(X, startInd_ho, endIndex_ho):
    """
    Function for forming X for the training proteins.
    Given a full X, we just need to delete part of full X, which is from holdout protein.
    :param X: a full X matrix with all proteins
    :param startInd_ho: first row in X to delete (held out data)
    :param endIndex_ho: last row in X to delete  (held out data)
    :return: return a X matrix without hold out protein X part
    """
    trainX = {}
    for j in range(MWID):
        trainX[j] = np.delete(X[j],range(startInd_ho,endIndex_ho+1), 0)  #0 means delete by rows
        trainX[j] = sparse.csr_matrix(trainX[j])
    return trainX


def formGLM_testX(X, index, modelType = 'classifier'):
    """
    Function for extracting X part from a full X just for hold out protein.
    :param X: a full X
    :param index: the index of the hold out protein in the unique protein list
    :return: a dictionary. Each base position j corresponds to the X part for hold out protein.
    X[j] is a 4 by E_j*20 matrix, where E_j the number of amino acids contact to the jth base position.
    """

    if modelType == 'classifier':
        testX = {}
        for j in range(MWID):
            testX[j] = X[j][(4*index):(4*index+4),]
        return testX
    elif modelType == 'regressor':
        testX = {}
        for j in range(MWID):
            testX[j] = X[j][index:index+1,]
        return testX        


def formGLM_Y(keysToUse):
    """
    Function for forming Y for either train or test Y, each protein is [A,T,C,G], which is [0,1,2,3] in code
    :param keysToUse: an array of proteins used
    :return: an array of repeated [0,1,2,3]. And note that len(Y)/4 is the number of proteins used.
    """
    n = len(keysToUse)
    Y = {}
    for j in range(MWID):
        Y[j] = np.array([0,1,2,3] * n)
    return Y


def formGLM_trainW(pwms, uniqueProteins, start, rev, modelType = 'classifier'):
    """
    Since we are using weighted multinomial logsitic regression, this function creates weights for
    trained proteins based on given position weight matrix, starting position, and orientation for each protein.
    :param pwms: a dictionary of {"protein": position weight matrix}
    :param uniqueProteins: an array of unique proteins
    :param S: a dictionary {"protein": starting position}
    :param O: a dictionary {"protein": orientation \in {0,1}}
    :return: W: a dictionary. Each base position j corresponds to an array of weights,
    every four numbers represent one protein's weights.
    """

    weights = {}
    for i, protein in enumerate(uniqueProteins):
        if rev[protein] == 1:
            pwm = matrix_compl(pwms[protein])
        else:
            pwm = pwms[protein]
        weights[protein] = {}
        for j in range(MWID):
            weights[protein][j] = pwm[j+start[protein]][:]

    #print weights

    W = {}
    if modelType == 'classifier':
        for j in range(MWID):
            W[j] = {}
            for i, protein in enumerate(uniqueProteins):
                W[j] = np.concatenate((W[j], weights[protein][j]), axis=None)
            W[j] = W[j][1:len(W[j])]
            # normalize weights for each W[j]
            W[j] = W[j] / sum(W[j])
    elif modelType == 'regressor':
        for j in range(MWID):
            W[j] = {}
            for base in BASE:
                W[j][base] = np.zeros(len(uniqueProteins), dtype = 'float')
                for i, protein in enumerate(uniqueProteins):
                    W[j][base][i] = weights[protein][j][B2IND[base]]
                #W[j][base] = W[j][base][1:len(W[j][base])]
                # normalize weights for each W[j]
                #W[j][base] = W[j][base] / sum(W[j][base])

    return W

def formGLM_testW(pwms, index, uniqueProteins, start, rev):
    """
    Function for forming weights for the hold out protein based on its position weight matrix, starting position,
    and orientation.
    :param pwms: a dictionary {"protein": position weight matrix}
    :param index: a scalar, index for hold out protein in the unique proteins
    :param uniqueProteins: an array of unique proteins
    :param start: a dictionary {"protein": starting position}
    :param rev: a dictionary {"protein": orientation \in {0,1}}
    :return: W: a dictionary, each jth base position corresponds to an array of length 4
    weights for the hold out protein
    """
    protein = uniqueProteins[index]
    if rev[protein] == 1:
        pwm = matrix_compl(pwms[protein])
    else:
        pwm = pwms[protein]
    testW = {}
    for j in range(MWID):
        testW[j] = pwm[j+start[protein]][:]
    return testW #testW = {postion j: 4*1 pwm probability}


def createGLMModel(trainX, trainY, trainW):
    """
    Function for performing eight weighted multinomial logistic regressions for eight base positions
    :param trainX: a dictionary of {base position: X matrix}
    :param trainY: a dictionary of {base position: y vector}
    :param trainW: a dictionary of {base position: weights, same length as y vector}
    :return:
    model: a dictionary of {base position: GLM model}
    """
    # Standarize features [Question]: I'm not sure that whether I should standardize X since X is very sparse
    #scaler = StandardScaler()
    #X_std = scaler.fit_transform(trainX)
    model = {}
    for j in range(MWID):
        #clf = LogisticRegression(fit_intercept=True, random_state=0, multi_class='multinomial', solver='newton-cg')
        clf = LogisticRegression(fit_intercept=True, random_state=0, 
                                 multi_class='multinomial', solver='newton-cg', 
                                 C = 1e9)
        model[j] = clf.fit(trainX[j], trainY[j], sample_weight=trainW[j])
    return model

def computeGLMLoglikelihood(testX, testW, model):
    """
    Function for computing log-likelihood at each base position based on each GLM model and sum them together
    :param testX: a dictionary of {base position: X matrix}
    :param testW: a dictionary of {base position: weights, same length as y vector}
    :param model: a dictionary of {base position: GLM model}
    :return:
    ll: log-likelihood for the given hold out protein and given W, which is based on certain
    starting position and orientation.
    """

    eps = 1e-30
    ll = [0] * MWID
    for j in range(MWID):
        # sum score across the base positions
        prediction = model[j].predict_proba(testX[j])[0]
  
        # multinomial and sample choice method
        n = SAMPLE
        sample = [int(round(x)) for x in testW[j]*n]
        ll[j] = np.dot(np.log(prediction), sample)

    return sum(ll)  

def compute_pearson_corr(testX, testW, model, corr_weight):
    # compute pearson correlation between predicted pwm and pwm given testW (given s and o) for each position
    # weighted sum up MWID pearson correlations
    corrs = [0]*MWID
    for j in range(MWID):
        pred = model[j].predict_proba(testX[j])
        corrs[j] = scipy.stats.pearsonr(pred[0], testW[j])[0]
    return np.dot(corrs, corr_weight)

def sampleStartPosGLM(testX, uniqueProteins, index, pwms, edges, model):
    """
    Function for updating the starting position and orientation for the holdout protein
    based on max log-likelihood computed.
    :param testX: a dictionary of {base position: X matrix}
    :param uniqueProteins: an array of unique proteins
    :param index: a scalar, the index of hold out protein in the unique proteins
    :param pwms: a dictionary {"protein": position weight matrix}
    :param edges: a dictionary of {base position: corresponding positions on the amino acid sequence}
    :param model: a dictionary of {base position: GLM model}
    :return:
    s: a new starting position for the holdout protein based on maximum log-likelihood
    o: a new orientation for the holdout protein based on maximum log-likelihood
    """
    
    holdout = uniqueProteins[index]
    pwm = pwms[holdout]
    seqWid = len(pwm)
    mWid = len(edges.keys())
    lls = []
    for r in range(2):
        for s in range(seqWid-mWid+1):
            start = {holdout:s}
            rev = {holdout:r}
            testW = formGLM_testW(pwms, index, uniqueProteins, start, rev)
            ll = computeGLMLoglikelihood(testX, testW, model)
            lls.append(ll)

    # lls = np.exp(np.array(lls-max(lls))/SAMPLE*5)
    lls = np.array(lls)
    lls -= lls.max() # To avoid underflow
    #print(lls)
    lls = np.exp(lls)
    #print(lls)
    elements = range(len(lls))
    s = np.random.choice(elements, 1, p=list(lls/sum(lls)))[0]

    r = 0
    if s >= len(lls)/2:
        r = 1
    s = s%(len(lls)/2)

    return s, r, lls

# debug function
def form_model(fullX, uniqueProteins, pwms, start, rev):
    X = fullX
    Y = formGLM_Y(uniqueProteins)
    W = formGLM_trainW(pwms, uniqueProteins, start, rev)
    model = createGLMModel(X, Y, W)
    return model

# debug function
def computePC_agree(model, uniqueProteins, pwms, start, rev, fullX):
    align_match = {}
    for j in range(MWID):
        match = 0
        for i, p in enumerate(uniqueProteins):
            testX = formGLM_testX(fullX, i)
            pred = model[j].predict_proba(testX[j])
            pwm = pwms[p]
            if rev[p] == 1:
                pwm = matrix_compl(pwm)
            true = pwm[j+start[p]][:]
            if scipy.stats.pearsonr(pred[0], true)[0] >= 0.5:
                match += 1
        align_match[j] = match/float(len(uniqueProteins))
    return align_match

def compute_MSE(model, uniqueProteins, pwms, start, rev, fullX):
    mse_dic = {}
    for j in range(MWID):
        mse = 0
        for i, p in enumerate(uniqueProteins):
            testX = formGLM_testX(fullX, i)
            pred = model[j].predict_proba(testX[j])
            pwm = pwms[p]
            if rev[p] == 1:
                pwm = matrix_compl(pwm)
            true = pwm[j+start[p]][:]
            mse += sklearn.metrics.mean_squared_error(true, pred[0])
        mse_dic[j] = mse/float(len(uniqueProteins))
    return mse_dic

def gibbsSampleGLM(pwms, edges, uniqueProteins, obsGrps, fullX, grpInd,
                   maxIters=25, randSeed=None, verbose=False, orientKey=None, 
                   orient=None, fixedStarts = {}, init_oracle=False):
    """
    Main algorithm function.
    Function for gibbs sampling using GLM.
    :param pwms: a dictionary {"protein": position weight matrix}
    :param edges: a dictionary of {base position: corresponding positions on the amino acid sequence}
    :param uniqueProteins: an array of unique proteins
    :param obsGrps: a dicitonary mapping core seqs to lists of uniqueProteins elements
    :param grpInd: a dictionary mapping core seqs to (start/end) row index tuples in full_X
    :param fullX: a dictionary of {base position: full X using all proteins}
    :param maxIters: the maximum iteration
    :param randSeed: random seed
    :param verbose: print or not
    :param orientKey: a protein we use its orientation as a criterion
    :param orient: 0 or 1
    :param a dictionary of fixed start/rev for subset and protiens
    :return:
    ll: logLikelihood of all the data under all the (converged) hidden params
    S: a dictionary of {protein: start position}
    O: a dictionary of {protein: orientation}
    reorient: true or false indicating reorient or not with respect to the orientKey
    """
    # Set the random seed again in case this is called by a parent process
    if randSeed is not None:
        np.random.seed(randSeed)
    else:
        np.random.seed()

    mWid = len(edges.keys())

    # Initialize starting positions and orientations for each protein
    # start: a dictionary {"str protein": int start position}
    # rev: a dictionary {"str protein": binary orientation}

    if init_oracle:
        with open('nbres.pickle') as f:
            nb_res = pickle.load(f)
        nb_ll = [x['ll'] for x in nb_res]
        nb_S = [x['start'] for x in nb_res]
        nb_O = [x['rev'] for x in nb_res]
        nb_opt = np.argmax(nb_ll)
        start = nb_S[nb_opt]
        rev = nb_O[nb_opt]
    else:
        start, rev = initStarts(uniqueProteins, pwms, mWid, fixedStarts = fixedStarts)


    init_model = form_model(fullX, uniqueProteins, pwms, start, rev)
    init_PC_agree = computePC_agree(init_model, uniqueProteins, pwms, start, rev, fullX)
    init_mse = compute_MSE(init_model, uniqueProteins, pwms, start, rev, fullX)

    # Alternate between model construction and latent variable updates
    # until the latent variables cease to change
    # converged = False
    nIters = 0
    if verbose:
        print("nIters: %d" %nIters)
        print("\tstarts:", start.values()[:20])
        print("\trevers:", rev.values()[:20])

    bestStart, bestRev, bestll = deepcopy(start), deepcopy(rev), -1e30
    llsAll = [bestll]
    converged = False
    while nIters < maxIters and not converged:
        #print "%d iterations" %nIters
        
        valuesChanged = 0
        # For each hold out group, compute GLM model based on all proteins except the hold out
        # group, then update the s, o for each held out protein using the GLM model obtained
        for grpID in obsGrps.keys():
            
            # Train on all but the held out group
            startInd_ho, endIndex_ho = grpInd[grpID]
            trainProteins = uniqueProteins[:startInd_ho/4] + \
                uniqueProteins[endIndex_ho/4+1:]
            trainX = formGLM_trainX(fullX,startInd_ho,endIndex_ho)
            trainY = formGLM_Y(trainProteins)
            trainW = formGLM_trainW(pwms, trainProteins, start, rev)
            model = createGLMModel(trainX, trainY, trainW)

            # Sample a new start/orientation for each held out protein 
            index_p = startInd_ho/4
            for p in obsGrps[grpID]:
                if p in fixedStarts.keys():
                    index_p += 1
                    continue
                testX = formGLM_testX(fullX, index_p)
                s, r, lls = sampleStartPosGLM(testX,uniqueProteins,index_p, 
                                              pwms, edges, model)
                if s != start[p] or r != rev[p]:
                    valuesChanged += 1
                start[p] = s
                rev[p] = r
                index_p += 1

        nIters += 1

        ll = 0
        for i, p in enumerate(uniqueProteins):
            testX = formGLM_testX(fullX, i)
            testW = formGLM_testW(pwms, i, uniqueProteins, start, rev)
            ll += computeGLMLoglikelihood(testX, testW, model)

        if verbose:
            print("nIters: %d" %nIters)
            print("--starts:", start.values()[:20])
            print("--revers:", rev.values()[:20])
            print("--loglik", ll)
            print("--valuesChanged: ", valuesChanged)
            agree = computePC_agree(model, uniqueProteins, pwms, start, rev, fullX)
            print("--perPositionFracAgree:")
            for pos in sorted(agree.keys()):
                print('\tpos%d: %f' %(pos, agree[pos]))
        
        if ll > bestll:
            bestll = ll
            bestStart = start
            bestRev = rev

        # Check for convergence
        llLen = len(llsAll)
        tol = 1.0
        nIterLTtol = 0
        for i in range(min(llLen, 5)):
            delta = abs(ll - llsAll[llLen-i-1])
            if delta < tol:
                nIterLTtol += 1
        if nIterLTtol == 5:
            converged = True
        llsAll.append(ll)

    start = bestStart
    rev = bestRev
    ll = bestll

    final_model = form_model(fullX, uniqueProteins, pwms, start, rev)
    PC_agree = computePC_agree(final_model, uniqueProteins, pwms, start, rev, fullX)
    final_mse = compute_MSE(final_model, uniqueProteins, pwms, start, rev, fullX)

    # Check whether the orientation of the key PWM is backwards
    reorient = False
    # No longer necessary bc I am fixing the orientaiton of KEY PWM
    #if rev[orientKey] != orient:
    #    reorient = True

    final_model = form_model(fullX, uniqueProteins, pwms, start, rev)
    PC_agree = computePC_agree(final_model, uniqueProteins, pwms, start, rev, fullX)

    return {'start': start, 'rev': rev, 'll': ll, "reorient": reorient,
            "PC_agree": PC_agree, "init_PC_agree": init_PC_agree, 
            "score": sum(PC_agree.values()), "init_mse": init_mse, 
            "final_mse": final_mse, "final_model": final_model}

def runGibbs(pwms, edges, uniqueProteins, obsGrps, fullX, grpInd, 
             verbose = False, kSamps = 25, orientKey = None, orient = None, 
             fixedStarts = {}):
    """ Runs the gibbsSampler routine K times using parallel executions
    """
    
    '''
    maxiter = MAXITER
    res = [gibbsSampleGLM(pwms, edges, uniqueProteins, obsGrps, fullX,
                          grpInd, maxiter, np.random.randint(0,1e9), 
                          verbose, orientKey, orient, fixedStarts, 
                          INIT_ORACLE)]
    return res
    '''

    #'''
    ncpus = multiprocessing.cpu_count()-1
    maxiter = MAXITER
    print("we will run", maxiter, " iterations.")
    p = multiprocessing.Pool(ncpus)
    procs = []
    for k in range(kSamps):
        args = (pwms, edges, uniqueProteins, obsGrps, fullX,
                grpInd, maxiter, np.random.randint(0,1e9),
                verbose, orientKey, orient, fixedStarts,INIT_ORACLE)
        procs.append(p.apply_async(gibbsSampleGLM, args=args))
    res = [x.get() for x in procs]
    return res
    #'''

def getFixedStarts_fromStructures(pwms, edges_hmmPos, core,
                                  verbose = False, outfile = None):
    '''
    # NOTE: No longer used due to limited GitHub space for structure files
    #       Use readSeedAlignment() instead
    Extracts the correct start and orientation within pwms for those with
    corresponding core sequences that are represented in the structural data.
    :param pwms: a dictionary {"protein": position weight matrix}
    :param edges: maps base positions to contacting aa core positons
    :param obsGrps: maps distinct core seqs to lists of protein names
    '''

    structContactDir = \
        '../../../../../pdb/txtSummaries/allAtomsQueryBall_origIndex/Homeobox/'
    structMatchStateFile = \
        '../../../../../pdb/txtSummaries/uniquenessScores/Homeobox.txt'
    nucChainDir = '../../../../../pdb/shilpaFiles/ligand/'

    # Get the complete set of core amino acid positions
    corepos = set()
    for bpos in edges_hmmPos:
        for aapos in edges_hmmPos[bpos]:
            corepos.add(aapos)
    corepos = sorted(list(corepos))
    print corepos

    # Read in the match state amino acids from the structures
    fin = open(structMatchStateFile, 'r')
    fin.readline()
    matches = {}
    for line in fin:
        l = line.strip().split()
        matches[l[0]] = l[2]
    fin.close()

    # Subset them to the core positions
    structCores = {}
    for m in matches.keys():
        #print matches[m][49]
        s = ''
        for pos in corepos:
            s += matches[m][pos-2]
        structCores[m] = s
    
    # Find the intersection between the cores from the structures 
    # and the cores from the PWMs
    isect_pwms = {}
    isect_structs = {}
    isect_structs_byID = []
    for struct in structCores.keys():
        if structCores[struct] not in core.values():
            continue
        isect_structs_byID.append(struct)
        identical = []
        for prot in core.keys():
            if structCores[struct] == core[prot]:
                identical.append(prot)
        isect_pwms[structCores[struct]] = identical
        if isect_structs.has_key(structCores[struct]):
            isect_structs[structCores[struct]].append(struct)
        else:
            isect_structs[structCores[struct]] = [struct]
    #print isect_pwms
    #print isect_structs
    #print isect_structs_byID, len(isect_structs_byID)

    # Can we find the conserved ASN_51 -> A contact at position 3
    # in at least on of the structures for this core sequence?
    foundKey = {}   # Maps from core sequence to structure with ASN51 -> A
    for coreSeq in isect_structs.keys():
        structIDs = isect_structs[coreSeq]
        for structID in structIDs:
            fin = open(structContactDir+structID+'.txt','r')
            fin.readline()
            for line in fin:
                l = line.strip().split('\t')
                mpos, bpos, chainID, cType, base, aa = l[0], l[1], l[2], l[3], l[4], l[6]
                if mpos == '49' and cType == 'non-bb' and aa == 'ASN':
                    if base == 'A':
                        foundKey[coreSeq] = \
                            {'struct': structID, 'chain': chainID, 'rc': False, 'bpos': int(l[1])}
        fin.close()

    # Maps coreSeq to its 6 position binding site in the structure, using a 
    # heuristic approach to correct the possibilty that ASN_51 contacts A2 
    # as well as A3 and thus the binding site is shifted by one
    bindSite = {}
    for coreSeq in foundKey.keys():#coreSeq = 'RRSRVSNAR'
        chainFile = nucChainDir+foundKey[coreSeq]['chain']+'.pdb'
        pdbRec = readPDBcoords(chainFile, excludeBackbone = False)
        startpos = foundKey[coreSeq]['bpos']-2
        if pdbRec[foundKey[coreSeq]['bpos']+1]['res'] == 'A' and \
            (pdbRec[foundKey[coreSeq]['bpos']-1]['res'] == 'T' or \
             pdbRec[foundKey[coreSeq]['bpos']-1]['res'] == 'C'):
            startpos += 1
        endpos = startpos + 6
        #print startpos, endpos
        #nucChain = ''.join([pdbRec[i]['res'] for i in sorted(pdbRec.keys())])
        maxPos = max(pdbRec.keys())
        nucChain = ''.join([pdbRec[i]['res'] for i in range(startpos,min(endpos,maxPos))])
        if len(nucChain) != 6:
            continue
        bindSite[coreSeq] = nucChain
        #print foundKey[coreSeq]['rc'], startpos, endpos, nucChain, isect_pwms[coreSeq]

    #print pwms['Pitx1'].shape
    # Create a length 6 numpy matrix for each core sequence
    structMats = {}
    for cs in bindSite.keys():
        m = np.zeros((6,4), dtype = 'float')
        for i, b in enumerate(bindSite[cs]):
            m[i,B2IND[b]] = 1
        structMats[cs] = m
        #print bindSite[cs]
        #print structMats[cs]


    # For each distinct core sequence, find the corresponding experimental
    # PWM with the best possible alignment to the structural (fake) PWM.  
    # If the alignment is above score theshold of XX.X, then fix the starting 
    # position and and orientation for the corresponding experimental PWM
    # to be the position and orientation of the best alignment
    bestAli = {}
    bestAli_all = {}
    for cs in structMats.keys():
        bestScore, bestExp, bestShift, bestOri = -6.0, '', 0, 0
        x = structMats[cs]
        for p in isect_pwms[cs]:
            #print x
            #print pwms[p]
            score, shift, ori = comp_matrices(x, pwms[p], minWidthM2 = 6)
            if score > bestScore:
                bestScore, bestShift, bestOri = score, shift, ori
                bestExp = p
        if bestOri:
            rev = 1
        else:
            rev = 0
        if bestScore >= 5:
            if verbose:
                print cs, bestScore, bestExp, bindSite[cs], bestShift, rev
            bestAli[cs] = {'pwm': bestExp, 'score': bestScore, 'shift': bestShift, 'rev': rev}
        bestAli_all[cs] = {'pwm': bestExp, 'score': bestScore, 'shift': bestShift, 'rev': rev}

    print len(isect_structs.keys()), len(bestAli.keys())
    fixedStarts = {}
    for cs in bestAli.keys():
        fixedStarts[bestAli[cs]['pwm']] = \
            {'start': -bestAli[cs]['shift'], 'rev': bestAli[cs]['rev']}
    #print core.values(), len(core.values())
    #print structCores.values(), len(structCores.values())
    #print corepos
    #print set(structCores.values()) & set(core.values())

    if outfile is not None:
        fout = open(outfile,'w')
        for p in fixedStarts.keys():
            fout.write('%s\t%d\t%d\n' %(p,fixedStarts[p]['start'],fixedStarts[p]['rev']))
        fout.close()
    return fixedStarts

def readSeedAlignment(infile):
    # Reads table of seed start/orientations (as created by getFixedStarts_fromStructures())
    fixedStarts = {}
    fin = open(infile)
    for line in fin:
        l = line.strip().split()
        fixedStarts[l[0]] = {'start': int(l[1]), 'rev': int(l[2])}
    fin.close()
    return fixedStarts

def getPrecomputedInputs():
    ##############################################
    # Under construction - do not use yet
    # Computes necessary data structures given PROT_SEQ_FILE, PWM_INPUT_TABLE,
    # HMM_FILE, HMM_LEN, HMM_NAME, HMM_OFFSET, and CONTACT_MAP
    
    # Get the PWM and protein info
    pwms = readPWMtab(PWM_INPUT_TABLE)
    prot = readFromFasta(PROT_SEQ_FILE)
    subsetDict(prot, set(pwms.keys()))
    
    # Get the contact map info
    edges_hmmPos = {}
    fin = open(CONTACT_MAP, 'r')
    fin.readline()
    aaPosList = set()
    for line in fin:
        bpos, aapos = tuple([int(x) for x in line.strip().split()])
        if bpos in edges_hmmPos:
            edges_hmmPos[bpos].append(aapos)
        else:
            edges_hmmPos[bpos] = [aapos]
        aaPosList.add(aapos)
    fin.close()
    aaPosList = sorted(list(aaPosList))
    edges = {}
    for bpos in edges_hmmPos:
        for aapos in edges_hmmPos[bpos]:
            if bpos in edges.keys():
                edges[bpos].append(aaPosList.index(aapos))
            else:
                edges[bpos] = [aaPosList.index(aapos)]

    # Convert to "core" contacting amino acids only
    corePos = [x - HMM_OFFSET for x in aaPosList]
    fasta = './tmp/seq_fasta_hasPWM.fa'
    writeToFasta(prot,'./tmp/seq_fasta_hasPWM.fa')
    hmmerout, matchTab = './tmp/hasPWM.hmmer3.out.txt', './tmp/hasPWM.matchTab.txt'
    runhmmer3(hmmerout, HMM_FILE, fasta, HMM_NAME, getdescs(fasta), 
              hmmerDir = HMMER_HOME)
    core, full, trunc = \
        makeMatchStateTab(hmmerout, matchTab, prot, HMM_LEN, HMM_NAME, corePos = corePos)

    # Removes test proteins/pwms from the dataset
    testProts = None
    if (EXCLUDE_TEST):
        fin = open(TEST_PROT_FILE,'r')
        testProts = [x.strip() for x in fin.readlines()]
        fin.close()
        for x in [pwms, core, full]:
            allProts = x.keys()
            for prot in testProts:
                try:
                    del x[prot]
                except KeyError:
                    next

    return pwms, core, full, edges, edges_hmmPos, aaPosList, testProts



def main():

    # Reproducible randomness
    mWid = MWID
    np.random.seed(RAND_SEED)

    if ORIGINAL_INPUT_FORMAT:
        pwms, core, full, edges, edges_hmmPos, aaPosList, testProts = \
            getTrainPairsAndInfo(rescalePWMs = RESCALE_PWMS, excludeTestSet = EXCLUDE_TEST)
    else:
        pwms, core, full, edges, edges_hmmPos, aaPosList, testProts = getPrecomputedInputs()
    assert MWID == len(edges.keys())
    
    """
    # Output directories used in manuscript
    if EXCLUDE_TEST:
        dir = "../results/cisbp-chu/"
    else:
        dir = "../results/cisbp-chuAll/"
    dir += "structFixed1_grpHoldout_multinomial_ORACLE"+str(INIT_ORACLE)+"Chain"+\
        str(N_CHAINS)+"Iter"+str(MAXITER)
    if RESCALE_PWMS:
        dir += 'scaled50'
    if not os.path.exists(dir):
        os.makedirs(dir)
    """

    # Output directory now set in by user
    dir = OUTPUT_DIRECTORY
    if not os.path.exists(dir):
        os.makedirs(dir)

    ## DEPRECATED but leaving for now ...
    trSet = 'cisbp'
    orientKey = ORIENT[trSet].keys()[0]
    orient = ORIENT[trSet][orientKey]
    ###

    print("number of proteins used", len(pwms.keys()))
    # Assign cores to similarity groups
    obsGrps = assignObsGrps(core, by = OBS_GRPS)
    with open(dir+'/obsGrpTab.txt', 'w') as fout:
        for k in sorted(obsGrps.keys()):
            for x in obsGrps[k]:
                fout.write('%s\t%s\n' %(k,x))

    print("Output written to: %s" %dir)
    # Write the PWMs used and other basic info to files
    makePWMtab(pwms, dir+'/pwmTab.txt')
    with open(dir+'/coreSeqTab.txt', 'w') as fout:
        fout.write('\n'.join(['%s\t%s' %(k,core[k]) \
                             for k in sorted(core.keys())]))

    uniqueProteins = pwms.keys()
    fullX, grpInd = formGLM_fullX(core, edges, uniqueProteins, obsGrps)
    
    #Sanity checks and setting up correct fixed unique protein ordering 
    for g in obsGrps.keys():
        assert len(obsGrps[g])*4 == grpInd[g][1]-grpInd[g][0]+1
    uprots = []
    for grp in obsGrps.keys():
        uprots += obsGrps[grp]
    assert len(uprots) == len(uniqueProteins)
    # So that uniqueProteins is in the same order as obsGrps keys
    uniqueProteins = uprots

    # Removed computation to save space for the GitHub
    #fixedStarts = getFixedStarts_fromStructures(pwms, edges_hmmPos, core, outfile = dir+'/fixedStarts.txt')
    fixedStarts = readSeedAlignment(SEED_FILE)
    print("We are using %d proteins in the gibbs sampling." %len(uniqueProteins))

    if RUN_GIBBS:
        print("Running %d markov chains ..." %N_CHAINS)
        startTime = time.time()
        res = runGibbs(pwms, edges, uniqueProteins, obsGrps, fullX, grpInd, 
                       verbose = False, kSamps = N_CHAINS, orientKey = orientKey, 
                       orient = orient, fixedStarts = fixedStarts)
        print("Ran in %.2f seconds" %(time.time()-startTime))
        ll = [x['ll'] for x in res]
        score = [x['ll'] for x in res]
        PC_agree = [x['PC_agree'] for x in res]
        init_PC_agree = [x['init_PC_agree'] for x in res]
        init_mse = [x['init_mse'] for x in res]
        final_mse = [x['final_mse'] for x in res]
        reorient = [x['reorient'] for x in res]
        start = [x['start'] for x in res]
        rev = [x['rev'] for x in res]
        opt = np.argmax(score)
        print("scores are", score)
        print("opt is", opt)
        print("initial score is", sum(init_PC_agree[opt].values()))
        print("final score is", sum(PC_agree[opt].values()))
        print("initial mse is", sum(init_mse[opt].values()))
        print("final mse is", sum(final_mse[opt].values()))
        np.savetxt(dir+"/init_PC_agree.out", init_PC_agree[opt].values())
        np.savetxt(dir+"/final_PC_agree.out", PC_agree[opt].values())
        np.savetxt(dir+"/init_mse.out", init_mse[opt].values())
        np.savetxt(dir+"/final_mse.out", final_mse[opt].values())
        print("Writing results in ", dir+'.pickle')
        with open(dir+'.pickle', 'wb') as f:
            pickle.dump(res, f)

if __name__ == '__main__':
    main()

