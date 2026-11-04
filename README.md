# FungiFunk_refdb

**A tool for building and maintaining reproducible protein reference databases from NCBI Datasets.**

FungiFunk_refdb is a command-line tool that creates protein reference databases from genome assemblies available through NCBI.

It allows users to choose which organisms and assemblies to include, retrieve their protein sequences, remove identical proteins, and keep track of the original sources of every protein in the database.

The tool also supports resuming interrupted runs, updating existing databases, and retrying failed NCBI downloads without rebuilding the entire reference.

**Current version:** v0.5.0  
**Status:** Active development

> **A note on the name:** FungiFunk_refdb was originally developed as part of the FungiFunk project. Despite its current name, the tool is not limited to fungi and can be used with other taxa. A future name change is planned to better reflect its broader scope.

## Key features

- **Create protein reference databases** from user-selected taxa and genome assemblies.
- **Customise assembly selection** using taxonomic information, assembly quality, strain representation, and other available metadata.
- **Remove duplicate proteins** while preserving information about all their original biological sources.
- **Resume interrupted runs** using saved progress.
- **Update existing references** while reusing previously processed data when possible.
- **Safely replace existing databases** only after the updated reference has been validated.
- **Retry failed NCBI protein downloads** in completed databases without performing a full update.
- **Generate detailed metadata and reports** describing database contents, selection decisions, and processing results.

## Installation

FungiFunk_refdb runs from the command line using Bash and Python.

A Conda environment configuration file, `FungiFunk_refdb.yml`, is provided in the root directory of the repository.

### 1. Get FungiFunk_refdb

Clone or download the repository and navigate to its main directory:

```bash
cd FungiFunk_refdb
```

### 2. Create the Conda environment

Make sure Conda is installed and available in your terminal.

Create an environment using the provided YAML file:

```bash
conda env create -f FungiFunk_refdb.yml -n FungiFunk_refdb
```

Activate it:

```bash
conda activate FungiFunk_refdb
```

### 3. Check the installation

Display the available options:

```bash
bash FungiFunk_refdb.sh --help
```

Check the software version:

```bash
bash FungiFunk_refdb.sh --version
```

The Bash script is the recommended entry point for running FungiFunk_refdb.

## Run modes

FungiFunk_refdb provides five mutually exclusive run modes:

| Run mode | Description |
| --- | --- |
| `--create` | Create a new reference database. |
| `--resume` | Resume an interrupted build from its persistent state. |
| `--create-new-updated-version` | Create an updated reference in a separate directory, preserving the original. |
| `--overwrite-with-updated-version` | Build an updated reference and safely replace the existing one after validation. |
| `--retry-ncbi-failures` | Retry eligible NCBI protein retrieval failures stored in a completed reference. |

Exactly one run mode must be selected for each execution.

## Getting started

The following examples illustrate how to run FungiFunk_refdb.

### 1. Create a new reference database

Create a database using a TSV file defining the taxa to include:

```bash
bash FungiFunk_refdb.sh \
    --create \
    --seed-taxa seed_taxa.tsv \
    --outdir reference_db
```

Here:

- `--create` starts a new database.
- `--seed-taxa` specifies the input TSV containing the requested taxa.
- `--outdir` determines where the database and its reports will be saved.

The output directory must be new or empty.

A seed-taxa file is not the only way to define the reference. Specific assemblies or strains can also be requested using `--include-explicit-targets`.

Input templates are available in the `FungiFunk_refdb_templates/` directory.

### 2. Resume an interrupted run

If a database-building run is interrupted after a suitable recovery checkpoint has been created, it can be resumed:

```bash
bash FungiFunk_refdb.sh \
    --resume \
    --outdir reference_db
```

FungiFunk_refdb uses its saved state to continue the interrupted operation without unnecessarily repeating completed work.

### 3. Create a new updated version

Create an updated reference while preserving the previous version:

```bash
bash FungiFunk_refdb.sh \
    --create-new-updated-version \
    --source-refdb reference_db \
    --outdir reference_db_updated
```

This mode creates the updated database in a separate directory.

By default, the biological selection settings are inherited from the original reference. These settings can be modified using the corresponding options.

### 4. Safely overwrite an existing reference

Update a database and replace the original version after successful validation:

```bash
bash FungiFunk_refdb.sh \
    --overwrite-with-updated-version \
    --outdir reference_db
```

The updated reference is built in a separate working directory.

The original database remains available while the update is being prepared and is replaced only after the new version has been completed and validated.

### 5. Retry failed NCBI protein downloads

*Introduced in v0.3.1.*

Occasionally, NCBI protein retrieval may fail for one or more assemblies. These failures can remain recorded in an otherwise completed database.

FungiFunk_refdb can retry eligible unresolved failures without performing a full database update:

```bash
bash FungiFunk_refdb.sh \
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

FungiFunk_refdb provides options to control which taxa and genome assemblies contribute proteins to the database.

### Selecting taxa

Use `--seed-taxa` to define the taxa to include.

Optionally, use `--exclude-taxa` to exclude specific taxa from the selection.

You can also provide files containing explicit strain or assembly inclusion and exclusion targets.

### Selecting genome assemblies

By default, FungiFunk_refdb considers eligible RefSeq (GCF) assemblies and uses a strain-based selection strategy.

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
FungiFunk_refdb_templates/
    fungal_seed_taxa.template.tsv
    fungal_exclusion_taxa.template.tsv
    metadata_filters.template.tsv
    explicit_targets.template.tsv
```

Some input and output filenames retain the original project naming. These names may be updated in future versions.

For a complete description of the available options and input formats, use:

```bash
bash FungiFunk_refdb.sh --help
```

## Output files

A completed FungiFunk_refdb run produces a protein reference database, associated metadata, reports, and saved state information.

### Protein reference

The main output is:

```text
database/
    FungiFunk_protein_refdb.faa
```

This FASTA file contains the unique protein sequences selected for the reference database.

### Metadata

The `metadata/` directory contains tables describing the selected organisms, assemblies, and proteins.

Some of the main files are:

```text
metadata/
    fungal_species_master.tsv
    fungal_assemblies_master.tsv
    fungal_protein_metadata.tsv
    fungal_protein_sources.tsv
    reproduction_targets.tsv
```

These tables make it possible to identify the original sources of the proteins included in the reference.

`reproduction_targets.tsv` records the exact assembly accessions selected for the completed database. It can be used as an input file when attempting to recreate a reference using the same assembly targets.

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

These files are managed automatically by FungiFunk_refdb.

## How does protein deduplication work?

Different assemblies, strains, or species may contain proteins with exactly the same amino acid sequence.

FungiFunk_refdb identifies these identical proteins and stores each unique sequence only once in the final FASTA file.

This reduces unnecessary duplication while preserving the biological information associated with each protein.

**Importantly, identical protein sequences are combined without losing their original sources.**

The accompanying metadata records all contributing protein sources, allowing users to trace reference proteins back to their original assemblies and organisms.

Only proteins with identical complete canonical amino acid sequences are combined.

## Reproducibility and database integrity

FungiFunk_refdb is designed to make reference construction reproducible and protect existing results.

It records the biological configuration, assembly selection, protein sources, and relevant processing information.

Interrupted runs can be resumed when a suitable recovery checkpoint is available.

Operations that replace an existing reference use a separate working directory and validate the new database before replacing the original.

Completed references include an integrity manifest and completion information to support verification.

## Project development

FungiFunk_refdb is under active development. Additional functionality, improvements, and documentation are planned for future versions.

Version **0.3.1** introduces targeted recovery of previously failed NCBI protein downloads from completed reference databases.

The project is also expected to be renamed in a future version to better reflect its applicability beyond fungi.

## Development and maintenance

FungiFunk_refdb was originally developed by **Blanca Candelas Galavis** as part of her PhD research in the [Rounge group](https://www.mn.uio.no/bils/english/groups/rounge-group/index.html) at the University of Oslo (UiO), Norway.

The tool is currently maintained by **Blanca Candelas Galavis**.

Additional contributors and future maintainers will be acknowledged here as the project evolves.

## License

FungiFunk_refdb is distributed under the **GNU General Public License, version 3 (GPLv3)**.

See the [LICENSE](LICENSE) file for the full license text.

## Citation

Formal citation information will be added as the project develops.

In the meantime, please include the software name and version when reporting its use in scientific work.