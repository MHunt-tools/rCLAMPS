# A sample file for how to make a prediction for a novel
# protein using the rCLAMPS homeodomain or C2H2-ZF model

from predictionExamples_helpers import *

# NOTE: Set DOMAIN_TYPE in predictionExamples_helpers.py
if DOMAIN_TYPE == 'homeodomain':
    MODEL_DIR = '../my_results/allHomeodomainProts/'   # Directory containing the rCLAMPS output for homeodomains
elif DOMAIN_TYPE == 'zf-C2H2':
    MODEL_DIR = '../my_results/zf-C2H2_250_50_seedFFSdiverse6/'   # Directory containing the rCLAMPS output for zf-C2H2s
elif DOMAIN_TYPE == 'TetR':
    MODEL_DIR = '../my_results/tetRProts117/'   # Directory containing the rCLAMPS output for the best TetR

# Directory containing a fasta of homeomdomain proteins to predict specificities for
PROTEIN_FILE = f'../examplePredictions/{DOMAIN_TYPE}/TetR_predictions.fa'  
OUTPUT_DIR = f'../examplePredictions/{DOMAIN_TYPE}/'
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

MAKE_LOGOS = True  # Set to True to make logos using WebLogo (False for only text PMWs)

def createFinalModel(domainType):
    # Currently, domainType should be either 'homeodomain' or 'zf-C2H2'

    # Import the training dataset and contact map information in the 
    # format used in '../precomputedInputs/'
    if domainType == 'homeodomain':
        pwms, core, full, edges, edges_hmmPos, aaPosList, testProts = getPrecomputedInputs()
    elif domainType == 'zf-C2H2':
        pwms, core, edges, edges_hmmPos, aaPosList = getPrecomputedInputs_zfC2H2()
    else:
        pwms, core, full, edges, edges_hmmPos, aaPosList, testProts = getPrecomputedInputs()

    #print(edges_hmmPos)

    # Retrieve the optimal offsets/orientations found previously by rCLAMPS
    filename = MODEL_DIR+'result.pickle'
    with open(filename, 'rb') as f:
        res = pickle.load(f)
    score = [x['ll'] for x in res]
    opt = np.argmax(score)
    start = [x['start'] for x in res][opt]
    rev = [x['rev'] for x in res][opt]
    [subsetDict(x, list(start.keys())) for x in [pwms, core]]

    # Assign to distinct observation groups
    obsGrps = assignObsGrps(core, by = OBS_GRPS)
    uprots = []
    for grp in list(obsGrps.keys()):
        uprots += obsGrps[grp]
    uniqueProteins = uprots  

    nDoms = {}
    for p in uniqueProteins:
        nDoms[p] = len(core[p])/len(aaPosList)

    # Train the model using the optimal offset found previously by rCLAMPS    
    fullX, grpInd = formGLM_fullX(core, edges, uniqueProteins, obsGrps, 
                                  domainOrder = DOMAIN_ORDER)
    model = form_model(fullX, uniqueProteins, nDoms, pwms, start, rev)
    return model, aaPosList, edges

def main():

    # Create a temporary directory
    if not os.path.exists('./tmp/'):
        os.makedirs('./tmp')

    # Create the model object and get the list of relevant match states
    print("Creating the model object ...")
    model, aaPosList, edges = createFinalModel(DOMAIN_TYPE)

    print("Converting fasta proteins to X vectors ...")
    # Read in proteins you are interested in predicting specificities for 
    # and create appropriate X matrices
    if DOMAIN_TYPE == 'homeodomain':
        fullX, uniqueProteins, obsGrps, grpInd = \
            getFullX_fromFasta_HMMer3(PROTEIN_FILE, aaPosList, edges)
    elif DOMAIN_TYPE == 'zf-C2H2':
        fullX, uniqueProteins, obsGrps, grpInd, nDoms = \
            getFullX_fromFile_zfC2H2(PROTEIN_FILE, aaPosList, edges)
    if DOMAIN_TYPE == 'TetR':
        fullX, uniqueProteins, obsGrps, grpInd = \
            getFullX_fromFasta_HMMer3(PROTEIN_FILE, aaPosList, edges)

    print("Making PWM predictions ...")
    # Make a prediction for each protein with a distinct combination 
    # of base-contatcing residues and store
    pred_pwms = {}
    for k, coreSeq in enumerate(list(grpInd.keys())):
        startInd_ho, endIndex_ho = grpInd[coreSeq]
        testProteins = []
        for prot in uniqueProteins:
            if prot in obsGrps[coreSeq]:
                testProteins += [prot]
        #print testProteins, coreSeq
        testX = formGLM_testX(fullX, startInd_ho, startInd_ho + 4)
        if DOMAIN_TYPE == 'homeodomain' or DOMAIN_TYPE == 'TetR':
            pwm = []
            for j in range(MWID):
                prediction = model[j].predict_proba(testX[j])[0].tolist()
                pwm.append(prediction)
        elif DOMAIN_TYPE == 'zf-C2H2':
            #print testProteins, coreSeq
            pwm = predictSpecificity_array_ZF(fullX, model, startInd_ho,
                                              nDoms[testProteins[0]], wtB1 = 0.5)
        for p in testProteins:
            pred_pwms[p] = np.array(pwm)

    # Output the specificity predictions in a table format
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
    makePWMtab(pred_pwms,OUTPUT_DIR+'/predicted_pwms.txt')

    if MAKE_LOGOS:
        print("Generating logos for each prediciton ...")
        makeLogos(pred_pwms, OUTPUT_DIR + '/predicted_logos/')

if __name__ == '__main__':
    main()