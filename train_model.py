
from __future__ import division
import os, sys
import time
import random
from progressbar import ProgressBar

import numpy as np
import pandas as pd

from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, r2_score

from mpds_client import MPDSDataRetrieval, MPDSExport

from mpds_ml_labs.prediction import get_descriptor, prop_semantics


def get_regr(a=None, b=None):

    if not a: a = 100
    if not b: b = 2

    return RandomForestRegressor(
        n_estimators=a,
        max_features=b,
        max_depth=None,
        min_samples_split=2, # recommended value
        min_samples_leaf=5, # recommended value
        bootstrap=True, # recommended value
        n_jobs=-1
    )


def estimate_quality(algo, args, values, attempts=30, nsamples=0.33):
    results = []
    for _ in range(attempts):
        X_train, X_test, y_train, y_test = train_test_split(args, values, test_size=nsamples)
        algo.fit(X_train, y_train)

        prediction = algo.predict(X_test)

        mae = mean_absolute_error(y_test, prediction)
        r2 = r2_score(y_test, prediction)
        results.append([mae, r2])

    results = list(map(list, zip(*results))) # transpose

    avg_mae = np.median(results[0])
    avg_r2 = np.median(results[1])
    return avg_mae, avg_r2


def mpds_get_data(prop_id, descriptor_kappa):
    """
    Fetch, massage, and save dataframe from the MPDS
    NB currently pressure is not taken into account!
    """
    print("Getting %s with descriptor kappa = %s" % (prop_semantics[prop_id]['name'], descriptor_kappa))
    starttime = time.time()

    client = MPDSDataRetrieval()

    props = client.get_dataframe(
        {"props": prop_semantics[prop_id]['name']},
        fields={'P': [
            'sample.material.chemical_formula',
            'sample.material.phase_id',
            'sample.measurement[0].property.scalar',
            'sample.measurement[0].property.units',
            'sample.measurement[0].condition[0].units',
            'sample.measurement[0].condition[0].name',
            'sample.measurement[0].condition[0].scalar'
        ]},
        columns=['Compound', 'Phase', 'Value', 'Units', 'Cunits', 'Cname', 'Cvalue']
    )
    props['Value'] = props['Value'].astype('float64') # to treat values out of bounds given as str
    props = props[np.isfinite(props['Phase'])]
    props = props[props['Units'] == prop_semantics[prop_id]['units']]
    props = props[
        (props['Value'] > prop_semantics[prop_id]['interval'][0]) & \
        (props['Value'] < prop_semantics[prop_id]['interval'][1])
    ]
    if prop_id not in ['m', 'd']:
        to_drop = props[
            (props['Cname'] == 'Temperature') & (props['Cunits'] == 'K') & ((props['Cvalue'] < 200) | (props['Cvalue'] > 400))
        ]
        print("Rows to neglect by temperature: %s" % len(to_drop))
        props.drop(to_drop.index, inplace=True)

    phases_compounds = dict(zip(props['Phase'], props['Compound'])) # keep the mapping for future
    avgprops = props.groupby('Phase')['Value'].mean().to_frame().reset_index().rename(columns={'Value': 'Avgvalue'})
    phases = np.unique(avgprops['Phase'].astype(int)).tolist()

    print("Got %s distinct crystalline phases" % len(phases))

    min_descriptor_len = 200
    max_descriptor_len = min_descriptor_len*10
    data_by_phases = {}

    print("Computing descriptors...")
    pbar = ProgressBar()
    for item in pbar(client.get_data(
        {
            "props": "atomic structure",
            "classes": "non-disordered"
        },
        fields={'S':['phase_id', 'entry', 'chemical_formula', 'cell_abc', 'sg_n', 'setting', 'basis_noneq', 'els_noneq']},
        phases=phases
    )):
        crystal = MPDSDataRetrieval.compile_crystal(item, 'ase')
        if not crystal: continue
        descriptor = get_descriptor(crystal, kappa=descriptor_kappa)

        if len(descriptor) < min_descriptor_len:
            descriptor = get_descriptor(crystal, kappa=descriptor_kappa, overreach=True)
            if len(descriptor) < min_descriptor_len:
                continue

        if len(descriptor) < max_descriptor_len:
            max_descriptor_len = len(descriptor)

        if item[0] in data_by_phases:
            left_len, right_len = len(data_by_phases[item[0]]), len(descriptor)

            if left_len != right_len: # align length
                data_by_phases[item[0]] = data_by_phases[item[0]][:min(left_len, right_len)]
                descriptor = descriptor[:min(left_len, right_len)]

            data_by_phases[item[0]] = (data_by_phases[item[0]] + descriptor)/2

        else:
            data_by_phases[item[0]] = descriptor

    for phase_id in data_by_phases.keys():
        data_by_phases[phase_id] = data_by_phases[phase_id][:max_descriptor_len]

    print("Current descriptor length: %d" % max_descriptor_len)

    structs = pd.DataFrame(list(data_by_phases.items()), columns=['Phase', 'Descriptor'])
    struct_props = structs.merge(avgprops, how='outer', on='Phase')
    struct_props = struct_props[struct_props['Descriptor'].notnull()]
    struct_props['Phase'] = struct_props['Phase'].map(phases_compounds)
    struct_props.rename(columns={'Phase': 'Compound'}, inplace=True)

    print("Done %s rows in %1.2f sc" % (len(struct_props), time.time() - starttime))

    struct_props.export_file = MPDSExport.save_df(struct_props, prop_id)
    print("Saving %s" % struct_props.export_file)

    return struct_props


def tune_model(data_file):
    """
    Load saved data and perform simple regressor parameter tuning
    """
    basename = data_file.split(os.sep)[-1]
    if basename.startswith('df') and basename[3:4] == '_' and basename[2:3] in prop_semantics:
        tag = basename[2:3]
        print("Detected property %s" % prop_semantics[tag]['name'])
    else:
        tag = None
        print("No property name detected")

    df = pd.read_pickle(data_file)

    X = np.array(df['Descriptor'].tolist())
    y = df['Avgvalue'].tolist()

    results = []
    for parameter_a in range(20, 501, 20):
        avg_mae, avg_r2 = estimate_quality(get_regr(a=parameter_a), X, y)
        results.append([parameter_a, avg_mae, avg_r2])
        print("%s\t\t\t%s\t\t\t%s" % (parameter_a, avg_mae, avg_r2))
    results.sort(key=lambda x: (-x[1], x[2]))

    print("Best result:", results[-1])
    parameter_a = results[-1][0]

    results = []
    for parameter_b in range(1, 13):
        avg_mae, avg_r2 = estimate_quality(get_regr(a=parameter_a, b=parameter_b), X, y)
        results.append([parameter_b, avg_mae, avg_r2])
        print("%s\t\t\t%s\t\t\t%s" % (parameter_b, avg_mae, avg_r2))
    results.sort(key=lambda x: (-x[1], x[2]))

    print("Best result:", results[-1])
    parameter_b = results[-1][0]

    print("a = %s b = %s" % (parameter_a, parameter_b))

    regr = get_regr(a=parameter_a, b=parameter_b)
    regr.fit(X, y)
    regr.metadata = {'mae': avg_mae, 'r2': round(avg_r2, 2)}

    if tag:
        export_file = MPDSExport.save_model(regr, tag)
        print("Saving %s" % export_file)


if __name__ == "__main__":
    try:
        arg = sys.argv[1]
    except IndexError:
        sys.exit(
    "What to do?\n"
    "Please, provide either a *prop_id* letter (%s) for a property data to be downloaded and fitted,\n"
    "or a data *filename* for tuning the model." % ", ".join(prop_semantics.keys())
        )
    try:
        descriptor_kappa = int(sys.argv[2])
    except:
        descriptor_kappa = None

    if arg in prop_semantics.keys():

        struct_props = mpds_get_data(arg, descriptor_kappa)

        X = np.array(struct_props['Descriptor'].tolist())
        y = struct_props['Avgvalue'].tolist()

        avg_mae, avg_r2 = estimate_quality(get_regr(), X, y)

        print("Avg. MAE: %.2f" % avg_mae)
        print("Avg. R2 score: %.2f" % avg_r2)

        tune_model(struct_props.export_file)

    elif os.path.exists(arg):
        tune_model(arg)

    else: raise RuntimeError("Unrecognized argument: %s" % arg)
