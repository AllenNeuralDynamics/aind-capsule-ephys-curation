import warnings

warnings.filterwarnings("ignore")

# GENERAL IMPORTS
import os
import argparse
import json
import numpy as np
from pathlib import Path
import time
import logging
from datetime import datetime, timedelta

# SPIKEINTERFACE
import spikeinterface as si
import spikeinterface.qualitymetrics as sqm

# AIND
from aind_data_schema.core.processing import DataProcess

try:
    from aind_log_utils import log
    HAVE_AIND_LOG_UTILS = True
except ImportError:
    HAVE_AIND_LOG_UTILS = False

URL = "https://github.com/AllenNeuralDynamics/aind-ephys-curation"
VERSION = "1.0"

data_folder = Path("../data/")
scratch_folder = Path("../scratch")
results_folder = Path("../results/")

# Define argument parser
parser = argparse.ArgumentParser(description="Curate ecephys data")

n_jobs_group = parser.add_mutually_exclusive_group()
n_jobs_help = "Duration of clipped recording in debug mode. Default is 30 seconds. Only used if debug is enabled"
n_jobs_help = (
    "Number of jobs to use for parallel processing. Default is -1 (all available cores). "
    "It can also be a float between 0 and 1 to use a fraction of available cores"
)
n_jobs_group.add_argument("static_n_jobs", nargs="?", default="-1", help=n_jobs_help)
n_jobs_group.add_argument("--n-jobs", default="-1", help=n_jobs_help)

params_group = parser.add_mutually_exclusive_group()
params_file_help = "Optional json file with parameters"
params_group.add_argument("static_params_file", nargs="?", default=None, help=params_file_help)
params_group.add_argument("--params-file", default=None, help=params_file_help)
params_group.add_argument("--params-str", default=None, help="Optional json string with parameters")


if __name__ == "__main__":
    ####### CURATION ########
    curation_notes = ""
    t_curation_start_all = time.perf_counter()

    args = parser.parse_args()

    N_JOBS = args.static_n_jobs or args.n_jobs
    N_JOBS = int(N_JOBS) if not N_JOBS.startswith("0.") else float(N_JOBS)
    PARAMS_FILE = args.static_params_file or args.params_file
    PARAMS_STR = args.params_str

    # Use CO_CPUS env variable if available
    N_JOBS_CO = os.getenv("CO_CPUS")
    N_JOBS = int(N_JOBS_CO) if N_JOBS_CO is not None else N_JOBS

    if PARAMS_FILE is not None:
        logging.info(f"\nUsing custom parameter file: {PARAMS_FILE}")
        with open(PARAMS_FILE, "r") as f:
            processing_params = json.load(f)
    elif PARAMS_STR is not None:
        processing_params = json.loads(PARAMS_STR)
    else:
        with open("params.json", "r") as f:
            processing_params = json.load(f)

    data_process_prefix = "data_process_curation"

    job_kwargs = processing_params["job_kwargs"]
    job_kwargs["n_jobs"] = N_JOBS
    si.set_global_job_kwargs(**job_kwargs)

    curation_params = processing_params["curation"]

    ecephys_sorted_folders = [
        p
        for p in data_folder.iterdir()
        if p.is_dir() and "ecephys" in p.name or "behavior" in p.name and "sorted" in p.name
    ]

    # look for subject and data_description JSON files
    subject_id = "undefined"
    session_name = "undefined"
    for f in data_folder.iterdir():
        # the file name is {recording_name}_subject.json
        if "subject.json" in f.name:
            with open(f, "r") as file:
                subject_id = json.load(file)["subject_id"]
        # the file name is {recording_name}_data_description.json
        if "data_description.json" in f.name:
            with open(f, "r") as file:
                session_name = json.load(file)["name"]

    if HAVE_AIND_LOG_UTILS:
        log.setup_logging(
            "Curate Ecephys",
            mouse_id=subject_id,
            session_name=session_name,
        )
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    logging.info("\nCURATION")

    # curation query
    isi_violations_ratio_thr = curation_params["isi_violations_ratio_threshold"]
    presence_ratio_thr = curation_params["presence_ratio_threshold"]
    amplitude_cutoff_thr = curation_params["amplitude_cutoff_threshold"]

    curation_query = f"isi_violations_ratio < {isi_violations_ratio_thr} and presence_ratio > {presence_ratio_thr} and amplitude_cutoff < {amplitude_cutoff_thr}"

    pipeline_mode = True
    if len(ecephys_sorted_folders) > 0:
        # capsule mode
        assert len(ecephys_sorted_folders) == 1, "Attach one sorted asset at a time"
        ecephys_sorted_folder = ecephys_sorted_folders[0]
        postprocessed_base_folder = ecephys_sorted_folder / "postprocessed"
        pipeline_mode = False
    elif (data_folder / "postprocessing_pipeline_output_test").is_dir():
        logging.info("\n*******************\n**** TEST MODE ****\n*******************\n")
        postprocessed_base_folder = data_folder / "postprocessing_pipeline_output_test"

        curation_query = (
            f"isi_violations_ratio < {isi_violations_ratio_thr} and amplitude_cutoff < {amplitude_cutoff_thr}"
        )
        del curation_params["presence_ratio_threshold"]
    else:
        curation_query = f"isi_violations_ratio < {isi_violations_ratio_thr} and presence_ratio > {presence_ratio_thr} and amplitude_cutoff < {amplitude_cutoff_thr}"
        postprocessed_base_folder = data_folder

    logging.info(f"Curation query: {curation_query}")
    curation_notes += f"Curation query: {curation_query}\n"

    if pipeline_mode:
        postprocessed_folders = [
            p for p in postprocessed_base_folder.iterdir() if "postprocessed_" in p.name
        ]
    else:
        postprocessed_folders = [
            p for p in postprocessed_base_folder.iterdir() if "postprocessed-sorting" not in p.name and p.is_dir()
        ]
    for postprocessed_folder in postprocessed_folders:
        datetime_start_curation = datetime.now()
        t_curation_start = time.perf_counter()
        if pipeline_mode:
            recording_name = ("_").join(postprocessed_folder.name.split("_")[1:])
        else:
            recording_name = postprocessed_folder.name
        if recording_name.endswith(".zarr"):
            recording_name = recording_name[:recording_name.find(".zarr")]
        curation_output_process_json = results_folder / f"{data_process_prefix}_{recording_name}.json"

        try:
            analyzer = si.load_sorting_analyzer_or_waveforms(postprocessed_folder)
            logging.info(f"Curating recording: {recording_name}")
        except Exception as e:
            logging.info(f"Spike sorting failed on {recording_name}. Skipping curation")
            # create an mock result file (needed for pipeline)
            mock_qc = np.array([], dtype=bool)
            np.save(results_folder / f"qc_{recording_name}.npy", mock_qc)
            continue

        # get quality metrics
        qm = analyzer.get_extension("quality_metrics").get_data()
        qm_curated = qm.query(curation_query)
        curated_unit_ids = qm_curated.index.values

        # flag units as good/bad depending on QC selection
        default_qc = np.array([True if unit in curated_unit_ids else False for unit in analyzer.sorting.unit_ids])
        n_passing = int(np.sum(default_qc))
        n_units = len(analyzer.unit_ids)
        logging.info(f"\t{n_passing}/{n_units} passing default QC.\n")
        curation_notes += f"{n_passing}/{n_units} passing default QC.\n"
        # save flags to results folder
        np.save(results_folder / f"qc_{recording_name}.npy", default_qc)
        t_curation_end = time.perf_counter()
        elapsed_time_curation = np.round(t_curation_end - t_curation_start, 2)

        # save params in output
        curation_params["recording_name"] = recording_name

        curation_outputs = dict(total_units=n_units, passing_qc=n_passing, failing_qc=n_units - n_passing)
        if pipeline_mode:
            curation_process = DataProcess(
                name="Ephys curation",
                software_version=VERSION,  # either release or git commit
                start_date_time=datetime_start_curation,
                end_date_time=datetime_start_curation + timedelta(seconds=np.floor(elapsed_time_curation)),
                input_location=str(data_folder),
                output_location=str(results_folder),
                code_url=URL,
                parameters=curation_params,
                outputs=curation_outputs,
                notes=curation_notes,
            )
            with open(curation_output_process_json, "w") as f:
                f.write(curation_process.model_dump_json(indent=3))

    t_curation_end_all = time.perf_counter()
    elapsed_time_curation_all = np.round(t_curation_end_all - t_curation_start_all, 2)
    logging.info(f"CURATION time: {elapsed_time_curation_all}s")
