# TAURIDAsProt

** <ins>T</ins>axon-guided <ins>A</ins>ssembly and <ins>U</ins>pdating of <ins>R</ins>eference databases through <ins>I</ins>dentity-based <ins>D</ins>eduplication of protein sequences derived from genome <ins>A</ins><ins>s</ins>semblies**

TAURIDAsProt is a command-line tool for building and maintaining protein reference databases using genome assemblies from NCBI Datasets.

You choose which organisms and assemblies to include. TAURIDAsProt retrieves their proteins, keeps each unique sequence only once, and records all the original sources of that sequence.

It can also resume interrupted runs, update existing references while reusing previous work, and retry failed NCBI downloads without repeating the full assembly search.

**Current version:** v0.6.0
**Status:** Active development

## Key features

- **Create protein reference databases** from user-selected taxa and genome assemblies.
- **Customise assembly selection** using taxonomic information, assembly quality, strain representation, and other available metadata.
- **Remove duplicate proteins** while preserving information about all their original biological sources.
- **Resume interrupted runs** using saved progress.
- **Update existing references** while reusing previously processed data when possible.
- **Safely replace existing databases** only after the updated reference has been validated.
- **Retry failed NCBI protein downloads** in completed databases without performing a full update.
- **Generate detailed metadata and reports** describing database contents, selection decisions, and processing results.
- **Save console output and run logs** so progress, warnings, and errors can be reviewed after an execution.

## Installation

TAURIDAsProt runs from the command line using Bash and Python.

A Conda environment configuration file, `TAURIDAsProt.yml`, is provided in the root directory of the repository.

### 1. Get TAURIDAsProt

Clone the repository and move into its directory:

```bash
git clone https://github.com/Rounge-lab/TAURIDAsProt.git
cd TAURIDAsProt
```

Alternatively, download the repository as a ZIP file, extract it, and open the extracted directory in your terminal.

### 2. Create the Conda environment

Make sure Conda is installed and available in your terminal.

Create an environment using the provided YAML file:

```bash
conda env create -f TAURIDAsProt.yml -n TAURIDAsProt
```

Activate it:

```bash
conda activate TAURIDAsProt
```

### 3. Check the installation

Display the available options:

```bash
bash TAURIDAsProt.sh --help
```

Check the software version:

```bash
bash TAURIDAsProt.sh --version
```

The Bash script is the recommended entry point for running TAURIDAsProt.

## Run modes

TAURIDAsProt provides five mutually exclusive run modes:

| Run mode | Description |
| --- | --- |
| `--create` | Create a new reference database. |
| `--resume` | Resume an interrupted build from its persistent state. |
| `--create-new-updated-version` | Create an updated reference in a separate directory, preserving the original. |
| `--overwrite-with-updated-version` | Build an updated reference and safely replace the existing one after validation. |
| `--retry-ncbi-failures` | Retry eligible NCBI protein retrieval failures stored in a completed reference. |

Exactly one run mode must be selected for each execution.

## Getting started

The following examples illustrate how to run TAURIDAsProt.

### 1. Create a new reference database

First, copy the taxon inclusion template:

```bash
cp TAURIDAsProt_templates/seed_taxa.template.tsv seed_taxa.tsv
```

Open `seed_taxa.tsv` and choose which organisms to include. The template contains commented examples to help you get started. You can uncomment and edit these examples, or add your own taxa.

Once your input file contains at least one active inclusion rule, create the reference:

```bash
bash TAURIDAsProt.sh \
    --create \
    --seed-taxa seed_taxa.tsv \
    --outdir reference_db
```

Here:

- `--create` starts a new database.
- `--seed-taxa` specifies the input TSV containing the requested taxa.
- `--outdir` determines where the database and its reports will be saved.

The output directory must be new or empty.

You can also create a reference using specific strains or genome assemblies, without a seed-taxa file.

Copy the explicit-target template:

```bash
cp TAURIDAsProt_templates/explicit_targets.template.tsv explicit_targets.tsv
```

Edit the file to include the strains or assemblies you want.

Then run:

```bash
bash TAURIDAsProt.sh \
    --create \
    --include-explicit-targets explicit_targets.tsv \
    --outdir explicit_reference_db
```

You can use `--seed-taxa`, `--include-explicit-targets`, or both.

All four input templates are available in the `TAURIDAsProt_templates/` directory.

### 2. Resume an interrupted run

If a database-building run is interrupted after a suitable recovery checkpoint has been created, it can be resumed:

```bash
bash TAURIDAsProt.sh \
    --resume \
    --outdir reference_db
```

TAURIDAsProt can resume an interrupted build from its last saved recovery point, once the initial setup has been completed and the build is ready for recovery.

For most stages, this means continuing from the last completed stage.

Two stages support more detailed, species-by-species recovery:

- **Assembly search and selection:** If interrupted, TAURIDAsProt keeps the assembly information already saved for completed species and continues with the remaining species.
- **Protein retrieval and processing:** If interrupted, TAURIDAsProt keeps successfully completed protein-processing results and continues with species that still need processing.

This means that interruptions during these two stages do not require repeating all the work completed earlier in the same stage.

Work that was interrupted before being safely saved may need to be repeated.

Some options determine which organisms and genome assemblies are included in the reference. TAURIDAsProt calls these its *biological configuration*. For example, they include:

- Taxa to include or exclude (`--seed-taxa`, `--exclude-taxa`, etc).
- Specific strains or assemblies (`--include-explicit-targets`, `--exclude-explicit-targets`).
- Assembly quality requirements (`--min-contig-n50`, `--require-busco`, etc).
- Assembly selection limits (`--max-strains-per-species`, `--max-assemblies-per-species`, etc).

These options can still be corrected when resuming a run, provided assembly search has not yet started. Once that stage begins, they are locked to keep the reference consistent.

Other settings, such as the number of workers (`--workers`) or the temporary directory (`--tmpdir`), can still be adjusted during resume when applicable.

For the complete list of biological configuration options, see the `BIOLOGICAL CONFIGURATION` section in `--help`.

### 3. Create a new updated version

Create an updated reference while preserving the previous version:

```bash
bash TAURIDAsProt.sh \
    --create-new-updated-version \
    --source-refdb reference_db \
    --outdir reference_db_updated
```

This mode creates the updated database in a separate directory.

By default, the biological selection settings are inherited from the original reference. These settings can be modified using the corresponding options.

### 4. Safely overwrite an existing reference

Update a database and replace the original version after successful validation:

```bash
bash TAURIDAsProt.sh \
    --overwrite-with-updated-version \
    --outdir reference_db
```

The updated reference is built in a separate working directory.

The original database remains available while the update is being prepared and is replaced only after the new version has been completed and validated.

### 5. Retry failed NCBI protein downloads

Occasionally, NCBI protein retrieval may fail for one or more assemblies. These failures can remain recorded in an otherwise completed database.

TAURIDAsProt can retry eligible unresolved protein-download failures without repeating the full NCBI assembly search:

```bash
bash TAURIDAsProt.sh \
    --retry-ncbi-failures \
    --source-refdb reference_db
```

This mode:

- Identifies previously recorded, eligible NCBI protein retrieval failures.
- Retries only the affected assemblies.
- Incorporates successfully recovered proteins.
- Recalculates the final assembly selection.
- Rebuilds and validates the updated reference before safely replacing the original.
- Leaves the existing reference unchanged if no assemblies are recovered.

The original biological configuration and database version are preserved.

Unlike the other run modes, `--retry-ncbi-failures` does not require `--outdir`.

## Customising your reference

TAURIDAsProt provides options to control which taxa and genome assemblies contribute proteins to the database.

### Selecting taxa

Use `--seed-taxa` to define the taxa to include.

Optionally, use `--exclude-taxa` to exclude specific taxa from the selection.

You can also provide files containing explicit strain or assembly inclusion and exclusion targets.

### Selecting genome assemblies

By default, TAURIDAsProt considers eligible RefSeq (GCF) assemblies and uses a strain-based selection strategy.

Some options for modifying assembly selection include:

- `--include-gca`: Also consider eligible GenBank (GCA) assemblies.
- `--include-isolates`: Extend selection to individual isolates.
- `--include-unassigned-assemblies`: Consider assemblies without strain or isolate information.
- `--max-strains-per-species`: Limit the number of represented strains.
- `--max-assemblies-per-species`: Use an alternative, more compact species-level selection strategy.

### Filtering assemblies

Additional filters can be applied using information such as:

- Assembly level and RefSeq category.
- Genome size and GC content.
- Contig and scaffold statistics.
- Number of protein-coding genes.
- BUSCO quality measurements.
- Assembly release date.
- Assembly and sequencing methods.
- User-defined metadata rules.

Optional numerical quality thresholds are not applied unless explicitly requested.

### Input templates

Templates for the main input files are available in:

```text
TAURIDAsProt_templates/
    seed_taxa.template.tsv
    exclusion_taxa.template.tsv
    metadata_filters.template.tsv
    explicit_targets.template.tsv
```

For a complete description of the available options and input formats, use:

```bash
bash TAURIDAsProt.sh --help
```

## Output files

A completed TAURIDAsProt run produces a protein reference database, associated metadata, reports, and saved state information.

### Protein reference

The main output is:

```text
database/
    TAURIDAsProt_protein_refdb.faa
```

This FASTA file contains the unique protein sequences selected for the reference database.

### Metadata

The `metadata/` directory contains tables describing the selected organisms, assemblies, and proteins.

Some of the main files are:

```text
metadata/
    species_master.tsv
    assemblies_master.tsv
    protein_metadata.tsv
    protein_sources.tsv
    protein_sources.yaml.gz
    reproduction_targets.tsv
```

These tables make it possible to identify the original sources of the proteins included in the reference.

`protein_sources.tsv` lists the original protein records contributing to the reference, including multiple sources for identical sequences.

`protein_sources.yaml.gz` provides grouped provenance information in a compressed format.

`reproduction_targets.tsv` contains the exact versioned genome assembly accessions selected for the completed reference.

You can use this file to create another reference from the same assembly targets, without preparing a new seed-taxa file:

```bash
bash TAURIDAsProt.sh \
    --create \
    --include-explicit-targets reference_db/metadata/reproduction_targets.tsv \
    --outdir reproduced_reference_db
```

This requests the same assemblies as the original reference. However, successful reproduction still depends on those assemblies and their protein sequences remaining available from NCBI.

### Reports

The `reports/` directory contains information about the database-building process:

```text
reports/
    build_summary.tsv
    resource_usage.tsv
    failed_downloads.tsv
    assembly_qc_failures.tsv
    protein_sequence_warnings.tsv
    species_without_protein_FASTA.tsv
```

Additional reports describe excluded taxa, filtering decisions, and changes introduced during database updates.

### Saved state

The `state/` directory stores the information required to track progress, resume interrupted runs, and verify completed references.

These files are managed automatically by TAURIDAsProt.

### Logs

TAURIDAsProt saves a record of what happens during each run.

The `logs/` directory contains records of each execution.

TAURIDAsProt automatically saves the console output to a file named `console_<run_id>.log`, allowing you to review progress messages, warnings, and errors after a run has finished or been interrupted.

The run information log (`build_info.log`) also records details such as the command options and software versions used.

For interrupted or unsuccessful updates, some logs may be kept in the temporary working directory or alongside the original reference. This allows you to investigate the problem without modifying a previously completed database.

## How does protein deduplication work?

Different assemblies, strains, or species may contain proteins with exactly the same amino acid sequence.

TAURIDAsProt identifies these identical proteins and stores each unique sequence only once in the final FASTA file.

This reduces unnecessary duplication while preserving the biological information associated with each protein.

**Importantly, identical protein sequences are combined without losing their original sources.**

The accompanying metadata records all contributing protein sources, allowing users to trace reference proteins back to their original assemblies and organisms.

Before comparing proteins, TAURIDAsProt removes FASTA formatting whitespace, converts letters to uppercase, and ignores a final translation-stop marker (`*`) when determining sequence identity. The original stop marker is still recorded in the provenance information.

Only proteins whose complete processed sequences are identical are combined. Similar but non-identical proteins remain separate.

## Resource use and safety

TAURIDAsProt can automatically choose the number of workers based on the available CPUs, memory, and disk space.

You can also choose the number of workers yourself using `--workers`. The `--force` option allows an explicitly requested worker count to exceed the program's recommendation, but it does not disable disk-space protection, integrity checks, or NCBI request limits.

When updating an existing reference, TAURIDAsProt uses a separate working directory so that the original database remains available until the new version is ready and validated.

## Reproducibility and database integrity

TAURIDAsProt is designed to make reference construction reproducible and protect existing results.

It records the biological configuration, assembly selection, protein sources, and relevant processing information.

Interrupted runs can be resumed when a suitable recovery checkpoint is available.

Operations that replace an existing reference use a separate working directory and validate the new database before replacing the original.

Completed references include an integrity manifest and completion information to support verification.

## Project development

TAURIDAsProt is under active development. Additional functionality, improvements, and documentation are planned for future versions.

## Development and maintenance

TAURIDAsProt was originally developed by **Blanca Candelas Galavis** as part of her PhD research in the [Rounge group](https://www.mn.uio.no/bils/english/groups/rounge-group/index.html) at the University of Oslo (UiO), Norway.

The tool is currently maintained by **Blanca Candelas Galavis**.

Additional contributors and future maintainers will be acknowledged here as the project evolves.

## License

TAURIDAsProt is distributed under the **GNU General Public License, version 3 (GPLv3)**.

See the [LICENSE](LICENSE) file for the full license text.

## Citation

Formal citation information will be added as the project develops.

In the meantime, please include the software name and version when reporting its use in scientific work.