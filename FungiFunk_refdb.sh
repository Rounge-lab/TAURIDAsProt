#!/usr/bin/env bash
# ==============================================================================
# FungiFunk_refdb.sh
# ==============================================================================

# Stop the script on command failures, undefined variables or hidden errors in pipes
set -euo pipefail

# Define the path to the location of the script
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PYTHON_SCRIPT="${script_dir}/FungiFunk_refdb.py"
TEMPLATE_DIR="${script_dir}/FungiFunk_refdb_templates"

# Define the name, version and final output file of the tool
TOOL_NAME="FungiFunk_refdb"
VERSION="0.3.1"
FINAL_FASTA_NAME="FungiFunk_protein_refdb.faa"

# Internal paths used by FungiFunk_refdb
REFDB_MARKER_REL="state/.fungifunk_refdb"
STATE_DB_REL="state/build_state.sqlite"
BUILD_MANIFEST_REL="state/build_manifest.tsv"
FINAL_FASTA_REL="database/${FINAL_FASTA_NAME}"
BASIC_LOG_REL="logs/build_info.log"
RESUME_READY_REL="state/.fungifunk_refdb_resume_ready.json"
COMPLETION_MARKER_REL="state/.fungifunk_refdb_complete.json"

# Preserve the original command-line arguments exactly as supplied
ORIGINAL_ARGS=("$@")

# Save a shell-escaped representation of the command for logging
COMMAND_RUN="$(printf '%q ' "$0" "${ORIGINAL_ARGS[@]}")"
COMMAND_RUN="${COMMAND_RUN% }"

# --timestamps is a run-local presentation option. Detect it before normal
# argument parsing so wrapper messages can use it regardless of option order.
TIMESTAMPS=false

for arg in "${ORIGINAL_ARGS[@]}"; do
    if [[ "$arg" == "--timestamps" ]]; then
        TIMESTAMPS=true
        break
    fi
done


log_msg() {
    if [[ "$TIMESTAMPS" == true && -n "$*" ]]; then
        printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
    else
        printf '%s\n' "$*"
    fi
}


########################################################################################################
# Help
########################################################################################################

Help() {
    echo " _____                  _ _____            _                  __     _ _          "
    echo "|  ___|   _ _ __   __ _(_)  ___|   _ _ __ | | __    _ __ ___ / _| __| | |__       "
    echo "| |_ | | | |  _ \ / _  | | |_ | | | |  _ \| |/ /   |  __/ _ \ |_ / _  | '_ \      "
    echo "|  _|| |_| | | | | (_| | |  _|| |_| | | | |   <    | | |  __/  _| (_| | |_) |     "
    echo "|_|   \__,_|_| |_|\__, |_|_|   \__,_|_| |_|_|\_\___|_|  \___|_|  \__,_|_.__/      "
    echo "                  |___/                                                           "
    echo ""
    echo "${TOOL_NAME} v${VERSION}: build and maintain the FungiFunk fungal protein reference database"
    echo ""
    echo "FungiFunk_refdb builds, resumes, or incrementally updates a reproducible fungal"
    echo "protein reference database from NCBI Datasets for downstream FungiFunk functional"
    echo "profiling."
    echo ""
    echo "Final protein reference:"
    echo "  ${FINAL_FASTA_NAME}"
    echo ""
    echo ""
    echo "##########################"
    echo "         USAGE            "
    echo "##########################"
    echo "  ${TOOL_NAME}.sh <run mode> [options]"
    echo ""
    echo "Exactly ONE run mode is required."
    echo ""
    echo "If no run mode is supplied, this help is shown and the command exits without"
    echo "starting a database build."
    echo ""
    echo ""
    echo "##########################"
    echo "       RUN MODES          "
    echo "##########################"
    echo "  --create"
    echo "      Create a completely NEW fungal protein reference database."
    echo ""
    echo "      Required:"
    echo "        --seed-taxa <file>"
    echo "        -o, --outdir <dir>"
    echo ""
    echo "      Optional:"
    echo "        --exclude-taxa <file>"
    echo "          Explicit taxonomic exclusions. If omitted, no taxa are excluded"
    echo "          by explicit exclusion rules."
    echo ""
    echo "      --create never resumes, cleans or overwrites an existing FungiFunk_refdb"
    echo "      build."
    echo ""
    echo "      The output directory must be new or empty."
    echo ""
    echo "      If the directory contains a resumable FungiFunk_refdb build, use --resume."
    echo ""
    echo "      If the directory contains a completed FungiFunk_refdb reference, use an"
    echo "      update mode rather than --create."
    echo ""
    echo "      A non-empty directory that is not recognized as FungiFunk_refdb is never"
    echo "      modified or cleaned automatically."
    echo ""
    echo ""
    echo "  --resume"
    echo "      Continue the SAME interrupted build from persistent FungiFunk_refdb state."
    echo ""
    echo "      Required:"
    echo "        -o, --outdir <dir>"
    echo ""
    echo "      --resume is available only after the initial persistent recovery checkpoint"
    echo "      has been committed."
    echo ""
    echo "      If a run failed before that checkpoint was created, --resume is not"
    echo "      available. Correct the original problem, manually remove the incomplete"
    echo "      output directory (or its contents), and start again with --create."
    echo ""
    echo "      FungiFunk_refdb never removes that incomplete directory automatically."
    echo ""
    echo "      Biological configuration is editable during pre-assembly resume, until"
    echo "      ASSEMBLY_SEARCH_AND_SELECTION begins."
    echo ""
    echo "      During this pre-assembly period:"
    echo "        - explicitly supplied biological options replace their stored values"
    echo "        - biological settings not supplied are preserved"
    echo "        - seed/exclusion/metadata-filter TSV snapshots may be replaced"
    echo "        - --reset-biological-config may return biology to clean defaults"
    echo "        - any biological change clears partial pre-assembly curation and"
    echo "          rebuilds TAXONOMY_CURATION from the beginning"
    echo ""
    echo "      No assembly/protein processing is lost when pre-assembly curation is rebuilt,"
    echo "      because assembly search and selection has not yet started."
    echo ""
    echo "      From ASSEMBLY_SEARCH_AND_SELECTION onward, biological"
    echo "      configuration is locked for that build."
    echo ""
    echo "      Post-lock resume reuses the committed biological configuration and work."
    echo "      If biological settings must change after this boundary, start a new build"
    echo "      with --create using a new or manually cleaned output directory."
    echo ""
    echo "      Computational/presentation settings may still be changed where applicable,"
    echo "      for example:"
    echo "        --workers"
    echo "        --force"
    echo "        --tmpdir"
    echo "        --verbose"
    echo "        --timestamps"
    echo ""
    echo "      --db-version is always locked during --resume because it identifies the"
    echo "      existing build."
    echo ""
    echo "      If the interrupted build originated from"
    echo "      --overwrite-with-updated-version, resume continues inside the saved staging"
    echo "      database. The previous valid reference remains untouched until the resumed"
    echo "      build has completed, passed final validation and is safely promoted."
    echo ""
    echo ""
    echo "  --create-new-updated-version"
    echo "      Incrementally update a completed FungiFunk_refdb database while preserving"
    echo "      the previous version unchanged."
    echo ""
    echo "      Required:"
    echo "        --source-refdb <dir>"
    echo "        -o, --outdir <dir>"
    echo ""
    echo "      --source-refdb must be a completed, validated FungiFunk_refdb reference."
    echo ""
    echo "      --source-refdb and --outdir MUST be different directories."
    echo ""
    echo "      By default, biological configuration is inherited from"
    echo "      the source reference. Explicit biological options supplied in the update"
    echo "      command override the corresponding inherited values."
    echo ""
    echo "      Use --reset-biological-config to discard inherited biological settings and"
    echo "      rebuild them from clean defaults."
    echo ""
    echo "      NCBI is queried again. Unchanged assemblies are reused where safe,"
    echo "      new or changed assemblies are processed, and sources that are removed or"
    echo "      no longer eligible are omitted from the new reference."
    echo ""
    echo "      Every update receives a new database version, supplied with --db-version"
    echo "      or generated automatically when that option is omitted."
    echo ""
    echo ""
    echo "  --overwrite-with-updated-version"
    echo "      Incrementally update a completed reference and replace it ONLY AFTER the"
    echo "      updated version has been fully staged and validated."
    echo ""
    echo "      Required:"
    echo "        -o, --outdir <dir>"
    echo ""
    echo "      --outdir identifies the existing completed reference to update."
    echo ""
    echo "      Biological configuration is inherited/reset using the same rules as"
    echo "      --create-new-updated-version."
    echo ""
    echo "      The existing valid database remains untouched while the update is built."
    echo "      A failed or interrupted update therefore leaves the previous reference usable."
    echo ""
    echo "      Safe staging temporarily requires additional disk space and may require"
    echo "      approximately the existing database size plus the new reference workspace"
    echo "      and normal worker temporary space."
    echo ""
    echo "  --retry-ncbi-failures"
    echo "    Retry unresolved NCBI protein retrieval failures recorded in an existing"
    echo "    completed FungiFunk_refdb reference."
    echo ""
    echo "    Required:"
    echo "        --source-refdb <dir>"
    echo ""
    echo "    This mode preserves the original biological configuration and db_version."
    echo "    It retries only eligible persisted NCBI failures; it does not perform a"
    echo "    new assembly inventory search or download unchanged assemblies."
    echo ""
    echo "    Recovered proteins are incorporated, final biological selection is"
    echo "    recalculated, and the reference is rebuilt and validated in isolated"
    echo "    staging before safely replacing the original."
    echo ""
    echo "    If no assemblies are recovered, the reference remains unchanged."
    echo "    Unresolved failures remain recorded for subsequent attempts."
    echo ""
    echo "    --outdir, --db-version, --resume and biological configuration changes"
    echo "    cannot be combined with this mode."
    echo ""
    echo "##########################"
    echo " BIOLOGICAL CONFIGURATION "
    echo "##########################"
    echo "In FungiFunk_refdb, biological configuration means the inputs and settings"
    echo "that determine which fungal taxa and genome assemblies can contribute"
    echo "proteins to the reference database."
    echo ""
    echo "The complete list of biological configuration options is:"
    echo ""
    echo "Taxon selection:"
    echo "  --seed-taxa"
    echo "  --exclude-taxa"
    echo ""
    echo "Custom species/assembly metadata rules:"
    echo "  --metadata-filters"
    echo ""
    echo "Explicit strain/assembly targets:"
    echo "  --include-explicit-targets"
    echo "  --exclude-explicit-targets"
    echo ""
    echo "Assembly source/scope:"
    echo "  --include-gca"
    echo "  --include-atypical"
    echo ""
    echo "Assembly QC and metadata filters:"
    echo "  --require-busco"
    echo "  --min-contig-n50"
    echo "  --max-contig-l50"
    echo "  --max-contigs"
    echo "  --min-scaffold-n50"
    echo "  --max-scaffold-l50"
    echo "  --max-scaffolds"
    echo "  --max-scaffold-gaps"
    echo "  --min-genome-size"
    echo "  --max-genome-size"
    echo "  --min-gc-percent"
    echo "  --max-gc-percent"
    echo "  --min-protein-coding-genes"
    echo "  --max-protein-coding-genes"
    echo "  --min-busco-complete"
    echo "  --max-busco-missing"
    echo "  --max-busco-fragmented"
    echo "  --max-busco-duplicated"
    echo "  --assembly-level"
    echo "  --released-after"
    echo "  --released-before"
    echo "  --refseq-category"
    echo "  --assembly-method-contains"
    echo "  --sequencing-technology-contains"
    echo ""
    echo "Assembly selection/depth:"
    echo "  --max-assemblies-per-strain"
    echo "  --max-strains-per-species"
    echo "  --include-isolates"
    echo "  --include-unassigned-assemblies"
    echo "  --max-assemblies-per-species"
    echo ""
    echo "--reset-biological-config controls whether these stored settings are inherited"
    echo "or reset. It is not itself an assembly-selection criterion."
    echo ""
    echo "The following options are NOT part of biological configuration:"
    echo "  --workers"
    echo "  --force"
    echo "  --tmpdir"
    echo "  --verbose"
    echo "  --db-version"
    echo "  --timestamps"
    echo ""
    echo ""
    echo "##########################"
    echo " BIOLOGICAL CONFIG RESET  "
    echo "##########################"
    echo "  --reset-biological-config"
    echo "      Discard the stored biological configuration and start from clean"
    echo "      biological defaults, then apply only the biological inputs and options"
    echo "      supplied in the current command."
    echo ""
    echo "      Required whenever this flag is used:"
    echo "        at least one of:"
    echo "          --seed-taxa <file>"
    echo "          --include-explicit-targets <file>"
    echo ""
    echo "      Valid with:"
    echo "        --create-new-updated-version"
    echo "        --overwrite-with-updated-version"
    echo "        pre-assembly --resume"
    echo ""
    echo "      Invalid with:"
    echo "        --create"
    echo "          --create already starts from clean biological defaults."
    echo ""
    echo "        post-lock --resume"
    echo "          Once ASSEMBLY_SEARCH_AND_SELECTION has started, biological"
    echo "          configuration is locked for that build."
    echo ""
    echo "      After a reset:"
    echo "        - supplied --seed-taxa is taken from the supplied file"
    echo "        - supplied --include-explicit-targets is taken from the supplied file"
    echo "        - omitted --exclude-taxa means no explicit taxonomic exclusions"
    echo "        - omitted --metadata-filters means no custom metadata-filter TSV"
    echo "        - omitted --exclude-explicit-targets means no explicit target exclusions"
    echo "        - omitted biological booleans return to false"
    echo "        - omitted named QC/filter values return to their clean defaults"
    echo ""
    echo "      During pre-assembly --resume, a reset preserves build identity, output"
    echo "      location, origin/source information and db_version. It resets only the"
    echo "      biological configuration."
    echo ""
    echo ""
    echo "##########################"
    echo "        ARGUMENTS         "
    echo "##########################"
    echo "Primary input files:"
    echo ""
    echo "  --seed-taxa <file>"
    echo "      Seed taxa and explicit species inclusion TSV."
    echo ""
    echo "      For --create and --reset-biological-config, at least one of"
    echo "      --seed-taxa or --include-explicit-targets is required."
    echo ""
    echo "      Optional for updates without --reset-biological-config."
    echo "      If omitted, the source reference's stored seed snapshot is inherited."
    echo "      If supplied, the new file replaces the inherited seed snapshot."
    echo ""
    echo "      Optional during pre-assembly --resume."
    echo "      If omitted, the existing build snapshot is preserved."
    echo "      If supplied, the new file replaces that snapshot and taxonomy curation"
    echo "      is rebuilt from the beginning."
    echo ""
    echo "      Not changeable after ASSEMBLY_SEARCH_AND_SELECTION has started."
    echo ""
    echo "      Template:"
    echo "        ${TEMPLATE_DIR}/fungal_seed_taxa.template.tsv"
    echo ""
    echo "  --exclude-taxa <file>"
    echo "      Explicit taxonomic exclusion TSV."
    echo ""
    echo "      Optional."
    echo ""
    echo "      During --create or --reset-biological-config, omission means no explicit"
    echo "      taxonomic exclusion rules."
    echo ""
    echo "      During an update without --reset-biological-config, or during a"
    echo "      pre-assembly --resume, omission preserves the stored exclusion snapshot."
    echo ""
    echo "      Supplying a new exclusion file replaces the stored/inherited snapshot."
    echo "      An explicitly empty exclusion file therefore means no explicit exclusions."
    echo ""
    echo "      Not changeable after ASSEMBLY_SEARCH_AND_SELECTION has started."
    echo ""
    echo "      Template:"
    echo "        ${TEMPLATE_DIR}/fungal_exclusion_taxa.template.tsv"
    echo ""
    echo "  --metadata-filters <file>"
    echo "      Optional advanced species/assembly metadata-filter TSV."
    echo ""
    echo "      During --create or --reset-biological-config, omission means no custom"
    echo "      metadata-filter TSV."
    echo ""
    echo "      During an update without --reset-biological-config or pre-assembly --resume,"
    echo "       omission preserves the stored metadata-filter snapshot when one exists."
    echo ""
    echo "      Supplying a new file replaces that snapshot."
    echo ""
    echo "      Not changeable after ASSEMBLY_SEARCH_AND_SELECTION has started."
    echo ""
    echo "      All active metadata-filter rules are combined using AND."
    echo "      Text matching is case-insensitive by default."
    echo "      Individual text rules may request Case_sensitive=TRUE."
    echo "      Missing metadata fails a filter when that active filter requires the field."
    echo ""
    echo "      Template:"
    echo "        ${TEMPLATE_DIR}/metadata_filters.template.tsv"
    echo ""
    echo "  --include-explicit-targets <file>"
    echo "      Explicit strain/assembly inclusion-target TSV."
    echo ""
    echo "      Supported target types:"
    echo "        strain"
    echo "          Requires Strain and at least one of Species or Taxid."
    echo "          FungiFunk selects the best usable eligible assembly for that strain."
    echo ""
    echo "        assembly"
    echo "          Requires an exact versioned GCF/GCA accession."
    echo "          Species, Taxid and Strain may be supplied as identity hints and are"
    echo "          validated against the resolved NCBI record."
    echo ""
    echo "      Explicit inclusions are additional to the normal automatic selection"
    echo "      limits. They do not consume automatic strain/assembly quota."
    echo ""
    echo "      An exact explicitly requested GCA may be admitted without globally"
    echo "      enabling --include-gca. This exception applies only to that target."
    echo ""
    echo "      May be supplied with --create, update modes, or a pre-assembly --resume."
    echo "      With --create or --reset-biological-config, it may satisfy the primary"
    echo "      inclusion requirement instead of --seed-taxa."
    echo ""
    echo "      Not changeable after ASSEMBLY_SEARCH_AND_SELECTION has started."
    echo ""
    echo "      Template shared with --exclude-explicit-targets:"
    echo "        ${TEMPLATE_DIR}/explicit_targets.template.tsv"
    echo ""
    echo "      Use separate TSV files for inclusions and exclusions when both options"
    echo "      are needed."
    echo ""
    echo "  --exclude-explicit-targets <file>"
    echo "      Explicit strain/assembly exclusion-target TSV."
    echo ""
    echo "      Uses the same target schema and template as --include-explicit-targets."
    echo "      Matching assemblies/strains cannot be selected and do not consume"
    echo "      automatic selection quota."
    echo ""
    echo "      Optional biological input."
    echo "      It cannot by itself satisfy the inclusion requirement for --create or"
    echo "      --reset-biological-config."
    echo ""
    echo "      Not changeable after ASSEMBLY_SEARCH_AND_SELECTION has started."
    echo ""
    echo "Paths and identifiers:"
    echo ""
    echo "  -o, --outdir <dir>"
    echo "      Target/current FungiFunk_refdb directory."
    echo "      Required in all run modes except --retry-ncbi-failures."
    echo ""
    echo "  --source-refdb <dir>"
    echo "      Completed and validated FungiFunk_refdb reference."
    echo "    Required by --create-new-updated-version and --retry-ncbi-failures."
    echo ""
    echo "    During manual NCBI recovery, this identifies the completed reference"
    echo "    that will be safely updated in place."
    echo ""
    echo "  --tmpdir <dir>"
    echo "      Optional alternative temporary workspace."
    echo "      This is a computational setting and may be changed during --resume."
    echo ""
    echo "  --db-version <string>"
    echo "      Optional human-readable database-version identity/provenance label."
    echo ""
    echo "      For --create and update modes, omission generates a version automatically."
    echo ""
    echo "      db_version is not part of biological configuration."
    echo ""
    echo "      During --resume, the original db_version is always preserved."
    echo "      Supplying --db-version with --resume is rejected."
    echo ""
    echo "General:"
    echo ""
    echo "  --verbose"
    echo "      Show detailed per-worker and per-assembly processing messages."
    echo "      Default output reports one concise result line per completed species."
    echo "      Verbose output additionally reports candidate-level processing details,"
    echo "      including accepted/rejected assemblies, source, download/package status,"
    echo "      protein FASTA availability, and worker completion summaries."
    echo "      This option affects console logging only. It does not change biological"
    echo "      reference selection and may be enabled or disabled freely on resume."
    echo ""
    echo "  --timestamps"
    echo "      Prefix textual runtime console messages with the current local time."
    echo "      Timestamp format: [HH:MM:SS]"
    echo "      One timestamp is added per logical message; continuation lines keep"
    echo "      their existing indentation."
    echo "      Blank lines, section banners, separators and the startup header are"
    echo "      intentionally left unchanged."
    echo "      This option affects console presentation only and may be enabled or"
    echo "      disabled freely on resume."
    echo ""
    echo "  --version"
    echo "      Show FungiFunk_refdb version and exit."
    echo ""
    echo "  -h, --help"
    echo "      Show this help message and exit."
    echo ""
    echo ""
    echo "##########################"
    echo "    ASSEMBLY SCOPE        "
    echo "##########################"
    echo "Default assembly scope:"
    echo "  Eligible normal GCF/RefSeq assemblies only."
    echo ""
    echo "  --include-gca"
    echo "      Also include eligible unpaired GCA/GenBank assemblies."
    echo ""
    echo "      If a GCA has a paired GCF present in the inventory, the GCF "
    echo "      representation is preferred."
    echo ""
    echo "  --include-atypical"
    echo "      Allow assemblies marked atypical by NCBI."
    echo "      Other hard eligibility and integrity rules still apply."
    echo ""
    echo ""
    echo "##########################"
    echo "     DEFAULT QC RULES     "
    echo "##########################"
    echo "By default FungiFunk_refdb excludes:"
    echo ""
    echo "  - NCBI atypical assemblies"
    echo "  - suppressed or withdrawn assemblies"
    echo "  - assemblies without a usable protein FASTA"
    echo "  - corrupt or invalid protein FASTA data"
    echo ""
    echo "No optional numerical assembly-quality threshold is active by default."
    echo ""
    echo ""
    echo "##########################"
    echo "   ASSEMBLY FILTERS       "
    echo "##########################"
    echo "Contig-level assembly statistics:"
    echo ""
    echo "  --min-contig-n50 <bp>           Minimum Contig N50"
    echo "  --max-contig-l50 <int>          Maximum Contig L50"
    echo "  --max-contigs <int>             Maximum number of contigs"
    echo ""
    echo "Scaffold-level assembly statistics:"
    echo ""
    echo "  --min-scaffold-n50 <bp>         Minimum Scaffold N50"
    echo "  --max-scaffold-l50 <int>        Maximum Scaffold L50"
    echo "  --max-scaffolds <int>           Maximum number of scaffolds"
    echo "  --max-scaffold-gaps <int>       Maximum number of scaffold gaps"
    echo ""
    echo "Genome properties:"
    echo ""
    echo "  --min-genome-size <bp>          Minimum genome size"
    echo "  --max-genome-size <bp>          Maximum genome size"
    echo "  --min-gc-percent <pct>          Minimum GC percentage"
    echo "  --max-gc-percent <pct>          Maximum GC percentage"
    echo ""
    echo "Annotation/gene content:"
    echo ""
    echo "  --min-protein-coding-genes <n>  Minimum number of protein-coding genes"
    echo "  --max-protein-coding-genes <n>  Maximum number of protein-coding genes"
    echo ""
    echo "BUSCO:"
    echo ""
    echo "  --min-busco-complete <pct>      Minimum BUSCO complete percentage"
    echo "  --max-busco-missing <pct>       Maximum BUSCO missing percentage"
    echo "  --max-busco-fragmented <pct>    Maximum BUSCO fragmented percentage"
    echo "  --max-busco-duplicated <pct>    Maximum BUSCO duplicated percentage"
    echo "  --require-busco                 Require BUSCO metadata to be available"
    echo ""
    echo "Assembly metadata:"
    echo ""
    echo "  --assembly-level <levels>"
    echo "      Restrict accepted assembly levels."
    echo "      Accepted syntax and values are validated by the Python implementation."
    echo ""
    echo "  --released-after <YYYY-MM-DD>"
    echo "      Keep assemblies released on or after this date."
    echo ""
    echo "  --released-before <YYYY-MM-DD>"
    echo "      Keep assemblies released on or before this date."
    echo ""
    echo "  --refseq-category <category>"
    echo "      Restrict assemblies by RefSeq category."
    echo ""
    echo "  --assembly-method-contains <text>"
    echo "      Keep assemblies whose assembly-method metadata contains <text>."
    echo "      Literal substring matching; case-insensitive."
    echo "      May be repeated; repeated values are combined using AND."
    echo ""
    echo "  --sequencing-technology-contains <text>"
    echo "      Keep assemblies whose sequencing-technology metadata contains <text>."
    echo "      Literal substring matching; case-insensitive."
    echo "      May be repeated; repeated values are combined using AND."
    echo ""
    echo "Assembly selection:"
    echo ""
    echo "Default strategy: strain-level representation."
    echo ""
    echo "  --max-assemblies-per-strain <int>"
    echo "      Maximum number of usable assemblies retained per represented strain."
    echo "      [default = 1]"
    echo ""
    echo "      Assemblies within each strain are ranked deterministically. If a"
    echo "      higher-ranked assembly does not provide usable proteins, lower-ranked"
    echo "      assemblies from the SAME strain are tried before moving to the next"
    echo "      strain."
    echo ""
    echo "  --max-strains-per-species <int|all>"
    echo "      Maximum number of usable strains represented per species."
    echo "      [default = all]"
    echo ""
    echo "      The limit counts strains that obtain at least one usable selected"
    echo "      assembly. A completely failed strain does not consume a strain slot."
    echo ""
    echo "  --include-isolates"
    echo "      Expand strain-level selection to represent distinct isolates."
    echo ""
    echo "      Within represented strains, each distinct isolate may contribute at"
    echo "      most one usable assembly. The strain representative may also satisfy"
    echo "      its isolate. Isolate-only records are also considered."
    echo ""
    echo "      Isolate expansion does not reintroduce a strain excluded by"
    echo "      --max-strains-per-species."
    echo ""
    echo "  --include-unassigned-assemblies"
    echo "      Also represent assemblies with neither strain nor isolate metadata."
    echo ""
    echo "      Such assemblies are grouped by BioSample when available; records"
    echo "      without a BioSample are treated as accession-specific groups."
    echo "      At most one usable assembly is retained per unassigned group."
    echo ""
    echo "Alternative compact species mode:"
    echo ""
    echo "  --max-assemblies-per-species <int>"
    echo "      Activate compact species-level selection and retain at most N usable"
    echo "      assemblies per species."
    echo ""
    echo "      This is an alternative selection strategy, not an additional cap on"
    echo "      strain-level selection."
    echo ""
    echo "      It cannot be combined with:"
    echo "        --max-assemblies-per-strain"
    echo "        --max-strains-per-species"
    echo "        --include-isolates"
    echo "        --include-unassigned-assemblies"
    echo ""
    echo "      Python validates these strategy conflicts authoritatively, including"
    echo "      conflicts with inherited biological configuration during updates."
    echo ""
    echo "Deterministic ranking:"
    echo ""
    echo "  Assemblies and strain representatives are ordered using the following"
    echo "  criteria, in this exact order of priority:"
    echo ""
    echo "    1. RefSeq category:"
    echo "         Reference genome > Representative genome > Other/missing"
    echo ""
    echo "    2. Assembly source:"
    echo "         GCF > GCA"
    echo ""
    echo "    3. Assembly level:"
    echo "         Complete genome > Chromosome > Scaffold > Contig > Unknown"
    echo ""
    echo "    4. BUSCO completeness:"
    echo "         Higher is preferred."
    echo ""
    echo "    5. Contig N50:"
    echo "         Higher is preferred."
    echo ""
    echo "    6. Scaffold N50:"
    echo "         Higher is preferred."
    echo ""
    echo "    7. Number of contigs:"
    echo "         Lower is preferred."
    echo ""
    echo "    8. Number of scaffolds:"
    echo "         Lower is preferred."
    echo ""
    echo "    9. Assembly accession:"
    echo "         Ascending lexicographic order (final deterministic tie-breaker)."
    echo ""
    echo "  Ranking is lexicographic: the first differing criterion determines which"
    echo "  candidate is preferred. Later criteria cannot compensate for differences"
    echo "  in earlier criteria."
    echo ""
    echo "  Missing numerical quality metrics rank after available values for that"
    echo "  metric. Missing values do not introduce additional QC cutoffs."
    echo ""
    echo "UPDATE MODES:"
    echo "  Previously selected assemblies may be reused when safe. New or changed"
    echo "  candidates are processed to establish protein FASTA availability."
    echo ""
    echo "  Reusable and newly processed candidates are then considered together when"
    echo "  the final assembly-selection policy is applied."
    echo ""
    echo "  Consequently, successful worker processing does NOT by itself imply final"
    echo "  reference inclusion. Assemblies deliberately omitted by the selection"
    echo "  policy are marked NOT_SELECTED rather than treated as protein failures."
    echo ""
    echo "  Protein provenance records and protein warnings associated with assemblies"
    echo "  not retained by final selection are removed from the new reference database."
    echo "  The source reference is not modified."
    echo ""
    echo ""
    echo "##########################"
    echo "        FILTERING         "
    echo "##########################"
    echo "Filtering follows this fixed precedence:"
    echo ""
    echo "  NCBI assembly inventory"
    echo "          |"
    echo "          v"
    echo "  GCF/GCA source scope"
    echo "          |"
    echo "          v"
    echo "  hard/default eligibility rules"
    echo "          |"
    echo "          v"
    echo "  named CLI filters"
    echo "          |"
    echo "          v"
    echo "  metadata_filters.tsv"
    echo "          |"
    echo "          v"
    echo "  eligible assembly pool"
    echo "          |"
    echo "          v"
    echo "  protein FASTA validation / safe reuse"
    echo "          |"
    echo "          v"
    echo "  deterministic assembly-selection policy"
    echo "  (strain-level default or compact species mode)"
    echo "          |"
    echo "          v"
    echo "  assemblies retained in the final reference"
    echo ""
    echo "Important:"
    echo ""
    echo "  - All active user filters are combined using AND."
    echo "  - Custom filters cannot resurrect assemblies removed by earlier hard rules."
    echo "  - If an active filter requires metadata that is missing, the entity fails"
    echo "    that filter and the missing-value reason is reported."
    echo "  - Filter summaries and detailed failure tables are written to the output."
    echo ""
    echo ""
    echo "##########################"
    echo "    METADATA FILTERS      "
    echo "##########################"
    echo "metadata_filters.tsv schema:"
    echo ""
    echo "  Level    Field    Operator    Value    Case_sensitive    Notes"
    echo ""
    echo "Supported levels:"
    echo ""
    echo "  species"
    echo "  assembly"
    echo ""
    echo "Supported operators:"
    echo ""
    echo "  present"
    echo "  absent"
    echo "  contains"
    echo "  not_contains"
    echo "  equals"
    echo "  not_equals"
    echo "  starts_with"
    echo "  ends_with"
    echo "  min"
    echo "  max"
    echo "  after"
    echo "  before"
    echo ""
    echo "Blank lines and lines whose first non-whitespace character is # are ignored."
    echo ""
    echo "Unknown metadata fields or operators are rejected rather than silently ignored."
    echo ""
    echo "FungiFunk_refdb uses a documented whitelist of normalized metadata fields."
    echo ""
    echo "Examples of species fields include:"
    echo "  Taxon_name"
    echo "  Taxid"
    echo "  Phylum"
    echo "  Class"
    echo "  Order"
    echo "  Family"
    echo "  Genus"
    echo "  Authority"
    echo "  Tier"
    echo "  Explicit_include"
    echo "  Gut_evidence"
    echo "  Human_evidence"
    echo ""
    echo "Examples of assembly fields include:"
    echo "  Assembly_accession"
    echo "  Assembly_source"
    echo "  Assembly_level"
    echo "  RefSeq_category"
    echo "  Strain"
    echo "  Isolate"
    echo "  BioProject"
    echo "  BioSample"
    echo "  Assembly_method"
    echo "  Sequencing_technology"
    echo "  Contig_N50"
    echo "  Contig_L50"
    echo "  Num_contigs"
    echo "  Scaffold_N50"
    echo "  Scaffold_L50"
    echo "  Num_scaffolds"
    echo "  Num_scaffold_gaps"
    echo "  Genome_size"
    echo "  GC_percent"
    echo "  Protein_coding_genes"
    echo "  BUSCO_complete"
    echo "  BUSCO_fragmented"
    echo "  BUSCO_missing"
    echo "  BUSCO_duplicated"
    echo "  Release_date"
    echo "  Protein_FASTA_available"
    echo ""
    echo "The Python implementation validates the authoritative supported field list."
    echo ""
    echo ""
    echo "##########################"
    echo "      RESOURCE CONTROL    "
    echo "##########################"
    echo "  --workers <auto|int>"
    echo "      Number of species-processing workers."
    echo "      [default = auto]"
    echo ""
    echo "      auto calculates a safe worker count using:"
    echo "        - available CPU"
    echo "        - available RAM"
    echo "        - temporary-disk space"
    echo "        - output/staging-disk space"
    echo ""
    echo "  --force"
    echo "      Override only FungiFunk_refdb's calculated local worker recommendation."
    echo ""
    echo "      --force requires an explicit numeric --workers value."
    echo ""
    echo "      --force NEVER disables:"
    echo "        - NCBI request-rate protection"
    echo "        - disk-exhaustion safeguards"
    echo "        - single-writer/output-integrity rules"
    echo "        - transactions and checkpoints"
    echo "        - FASTA validation"
    echo "        - exact full-sequence verification"
    echo ""
    echo ""
    echo "##########################"
    echo "    PROTEIN IDENTITY      "
    echo "##########################"
    echo "FungiFunk_refdb performs exact canonical amino-acid sequence deduplication."
    echo ""
    echo "Protein sequence normalization:"
    echo ""
    echo "  - remove FASTA formatting whitespace and newlines"
    echo "  - convert letters to uppercase"
    echo "  - remove a terminal translation-stop marker (*) from canonical peptide identity"
    echo "  - record the original terminal stop in provenance"
    echo "  - preserve internal * symbols and report them"
    echo "  - otherwise preserve amino-acid symbols without replacement or inference"
    echo ""
    echo "Exact identity is determined using SHA-256-assisted lookup followed by complete"
    echo "amino-acid sequence comparison."
    echo ""
    echo "Only proteins that are 100% identical over their complete canonical sequence are"
    echo "collapsed."
    echo ""
    echo ""
    echo "##########################"
    echo "         OUTPUTS          "
    echo "##########################"
    echo "Main protein reference:"
    echo ""
    echo "  database/"
    echo "      ${FINAL_FASTA_NAME}"
    echo ""
    echo "Metadata:"
    echo ""
    echo "  metadata/"
    echo "      fungal_seed_taxa_resolved.tsv"
    echo "      fungal_exclusion_taxa_resolved.tsv"
    echo "      metadata_filters_resolved.tsv"
    echo "      fungal_species_master.tsv"
    echo "      fungal_species_master.xlsx              [if openpyxl is available]"
    echo "      fungal_assemblies_master.tsv"
    echo "      fungal_assemblies_master.xlsx           [if openpyxl is available]"
    echo "      fungal_protein_metadata.tsv"
    echo "      fungal_protein_sources.tsv"
    echo ""
    echo "Reports:"
    echo ""
    echo "  reports/"
    echo "      excluded_taxa.tsv"
    echo "      species_filter_failures.tsv"
    echo "      assembly_qc_failures.tsv"
    echo "      protein_sequence_warnings.tsv"
    echo "      species_without_protein_FASTA.tsv"
    echo "      failed_downloads.tsv"
    echo "      reference_update_changes.tsv      [update runs]"
    echo "      resource_usage.tsv"
    echo "      build_summary.tsv"
    echo ""
    echo "Persistent state:"
    echo ""
    echo "  state/"
    echo "      .fungifunk_refdb"
    echo "          Marks the directory as a recognized FungiFunk_refdb build."
    echo ""
    echo "      .fungifunk_refdb_resume_ready.json"
    echo "          Written only after enough persistent state exists for safe --resume."
    echo ""
    echo "      build_state.sqlite"
    echo "          Transactional build state and committed checkpoints."
    echo ""
    echo "      build_manifest.tsv"
    echo "          Final cryptographic inventory of completed reference contents."
    echo ""
    echo "      .fungifunk_refdb_complete.json"
    echo "          Final completion marker written only after the manifest is verified."
    echo ""
    echo "A SQLite state of DONE by itself does not define an officially completed"
    echo "reference. Final completion requires the validated completion marker."
    echo ""
    echo "TSV files are plain-text, tab-separated tables intended for downstream"
    echo "analysis and scripting. XLSX copies, where provided, are convenience files"
    echo "for manual inspection."
    echo ""
    echo ""
    echo "##########################"
    echo "    UPDATE / RESUME       "
    echo "##########################"
    echo "FungiFunk_refdb uses persistent state and explicit checkpoints so interrupted"
    echo "builds can be recovered conservatively."
    echo ""
    echo "Recovery rules:"
    echo ""
    echo "  1. --create always starts a NEW build."
    echo ""
    echo "  2. A recognized build with a completed recovery checkpoint should be"
    echo "     continued with --resume rather than rerun with --create."
    echo ""
    echo "  3. If initialization failed before a resumable checkpoint was created,"
    echo "     --resume is unavailable. Correct the problem, manually remove the"
    echo "     incomplete output directory (or its contents), and rerun --create."
    echo ""
    echo "  4. FungiFunk_refdb never automatically deletes an incomplete output"
    echo "     directory or unrelated user files."
    echo ""
    echo "Biological lock boundary:"
    echo ""
    echo "  Before ASSEMBLY_SEARCH_AND_SELECTION begins:"
    echo "      biological configuration remains editable"
    echo ""
    echo "      no biological change"
    echo "          -> completed taxonomy curation may be reused"
    echo ""
    echo "      biological change"
    echo "          -> partial/completed pre-assembly curation is cleared"
    echo "          -> TAXONOMY_CURATION is rebuilt from the beginning"
    echo ""
    echo "  From ASSEMBLY_SEARCH_AND_SELECTION onward:"
    echo "      biological configuration is locked"
    echo "      committed assembly/protein work is resumed using the saved configuration"
    echo ""
    echo "      workers/tmpdir/force/verbose/timestamps may still change where applicable"
    echo ""
    echo "      db_version never changes during --resume"
    echo ""
    echo "Incremental updates compare the current NCBI inventory with the previous"
    echo "completed reference."
    echo ""
    echo "Assemblies are classified as:"
    echo ""
    echo "  KEPT"
    echo "      unchanged assembly/protein sources that can be reused safely"
    echo ""
    echo "  ADDED"
    echo "      new or newly eligible assemblies"
    echo ""
    echo "  UPDATED"
    echo "      assemblies whose relevant assembly/annotation fingerprint has changed"
    echo ""
    echo "  REMOVED"
    echo "      an assembly that contributed proteins to the previous reference but does"
    echo "      not contribute proteins to the updated reference."
    echo ""
    echo "      This can happen when:"
    echo "        - its species is no longer retained by the current taxonomic selection"
    echo "          or species-level filters"
    echo "        - its species is still retained, but the previously selected assembly"
    echo "          accession is no longer returned in the current NCBI assembly inventory"
    echo "          for that species"
    echo "        - the assembly is present in the current NCBI inventory but no longer"
    echo "          passes the active source-scope, eligibility, QC or metadata rules"
    echo "        - the assembly remains eligible but is not retained by the active"
    echo "          assembly-selection policy"
    echo "        - a previously contributing assembly requires new processing but does not"
    echo "          provide a usable protein FASTA for the updated reference"
    echo ""
    echo "The final monolithic FASTA is regenerated safely from the resulting canonical"
    echo "sequence and provenance state."
    echo ""
    echo "For --overwrite-with-updated-version:"
    echo ""
    echo "  - the existing valid reference is not modified during staging"
    echo "  - the updated reference is validated before promotion"
    echo "  - failure leaves the previous valid reference usable"
    echo "  - a resumable interrupted staging build may be continued with --resume"
    echo "  - a staging build interrupted before the recovery checkpoint must be removed"
    echo "    manually before restarting the overwrite update"
    echo "  - a successfully resumed overwrite replaces the old reference only after"
    echo "    final validation"
    echo ""
    echo "Completion contract:"
    echo ""
    echo "  - build_state.sqlite records transactional progress"
    echo "  - DONE is written before the final manifest is generated"
    echo "  - build_manifest.tsv inventories the final reference"
    echo "  - the manifest is verified"
    echo "  - .fungifunk_refdb_complete.json is written last"
    echo ""
    echo "Therefore DONE without a valid completion marker is treated as an interrupted"
    echo "final commit and remains recoverable rather than being accepted as complete."
    echo ""
    echo ""
    echo "##########################"
    echo "         EXAMPLES         "
    echo "##########################"
    echo "-> Create a new reference:"
    echo ""
    echo "  ${TOOL_NAME}.sh \\"
    echo "      --create \\"
    echo "      --seed-taxa fungal_seed_taxa.tsv \\"
    echo "      --exclude-taxa fungal_exclusion_taxa.tsv \\"
    echo "      --outdir FungiFunk_refdb"
    echo ""
    echo ""
    echo "-> Create a new reference with broader assembly source scope:"
    echo ""
    echo "  ${TOOL_NAME}.sh \\"
    echo "      --create \\"
    echo "      --seed-taxa fungal_seed_taxa.tsv \\"
    echo "      --exclude-taxa fungal_exclusion_taxa.tsv \\"
    echo "      --outdir FungiFunk_refdb \\"
    echo "      --include-gca"
    echo ""
    echo ""
    echo "-> Create a strain-level reference with isolate and unassigned expansion:"
    echo ""
    echo "  ${TOOL_NAME}.sh \\"
    echo "      --create \\"
    echo "      --seed-taxa fungal_seed_taxa.tsv \\"
    echo "      --outdir FungiFunk_refdb_depth \\"
    echo "      --max-strains-per-species all \\"
    echo "      --include-isolates \\"
    echo "      --include-unassigned-assemblies"
    echo ""
    echo ""
    echo "-> Require Illumina + PacBio metadata and Contig N50 >= 50 kb:"
    echo ""
    echo "  ${TOOL_NAME}.sh \\"
    echo "      --create \\"
    echo "      --seed-taxa fungal_seed_taxa.tsv \\"
    echo "      --exclude-taxa fungal_exclusion_taxa.tsv \\"
    echo "      --outdir FungiFunk_refdb_custom \\"
    echo "      --sequencing-technology-contains illumina \\"
    echo "      --sequencing-technology-contains pacbio \\"
    echo "      --min-contig-n50 50000"
    echo ""
    echo ""
    echo "-> Resume an interrupted build using four workers:"
    echo ""
    echo "  ${TOOL_NAME}.sh \\"
    echo "      --resume \\"
    echo "      --outdir FungiFunk_refdb \\"
    echo "      --workers 4"
    echo ""
    echo ""
    echo "-> Correct biological configuration during a pre-assembly resume:"
    echo ""
    echo "  ${TOOL_NAME}.sh \\"
    echo "      --resume \\"
    echo "      --outdir FungiFunk_refdb \\"
    echo "      --seed-taxa corrected_fungal_seed_taxa.tsv \\"
    echo "      --include-gca"
    echo ""
    echo "  Because assembly search has not started, the supplied biological changes are"
    echo "  accepted and TAXONOMY_CURATION is rebuilt from the beginning."
    echo ""
    echo ""
    echo "-> Reset biological configuration during a pre-assembly resume:"
    echo ""
    echo "  ${TOOL_NAME}.sh \\"
    echo "      --resume \\"
    echo "      --outdir FungiFunk_refdb \\"
    echo "      --reset-biological-config \\"
    echo "      --seed-taxa corrected_fungal_seed_taxa.tsv"
    echo ""
    echo "  The existing build identity and db_version are preserved, while biological"
    echo "  configuration returns to clean defaults before the supplied inputs are"
    echo "  applied."
    echo ""
    echo ""
    echo "-> Re-create a reference from the exact selected-assembly targets of a"
    echo "   previously completed reference:"
    echo ""
    echo "  ${TOOL_NAME}.sh \\"
    echo "      --create \\"
    echo "      --outdir reproduced_refdb \\"
    echo "      --include-explicit-targets \\"
    echo "          previous_refdb/metadata/reproduction_targets.tsv"
    echo ""
    echo "  metadata/reproduction_targets.tsv is generated automatically in every"
    echo "  completed reference. It contains one exact versioned assembly target for"
    echo "  each final selected assembly."
    echo ""
    echo "  This requests the same selected assembly accessions without requiring a"
    echo "  seed-taxonomy file. Successful representation still depends on the target"
    echo "  assemblies remaining retrievable and usable."
    echo ""
    echo ""
    echo "-> Create a new updated version while inheriting the previous configuration:"
    echo ""
    echo "  ${TOOL_NAME}.sh \\"
    echo "      --create-new-updated-version \\"
    echo "      --source-refdb FungiFunk_refdb \\"
    echo "      --outdir FungiFunk_refdb_v2"
    echo ""
    echo ""
    echo "-> Create a new updated version from clean configuration:"
    echo ""
    echo "  ${TOOL_NAME}.sh \\"
    echo "      --create-new-updated-version \\"
    echo "      --source-refdb FungiFunk_refdb \\"
    echo "      --outdir FungiFunk_refdb_v2 \\"
    echo "      --reset-biological-config \\"
    echo "      --seed-taxa fungal_seed_taxa.tsv \\"
    echo "      --exclude-taxa fungal_exclusion_taxa.tsv \\"
    echo "      --include-gca"
    echo ""
    echo ""
    echo "-> Safely replace a completed database after an incremental update:"
    echo ""
    echo "  ${TOOL_NAME}.sh \\"
    echo "      --overwrite-with-updated-version \\"
    echo "      --outdir FungiFunk_refdb"
    echo ""
    echo ""
    echo "-> Retry previously failed NCBI protein downloads in a completed reference:"
    echo ""
    echo "FungiFunk_refdb.sh \\"
    echo "    --retry-ncbi-failures \\"
    echo "    --source-refdb FungiFunk_refdb"
    echo ""
    echo "Biological configuration and database version are preserved."
    echo "The original reference remains unchanged if no proteins are recovered."
    echo ""
    echo ""
    echo "##########################"
    echo "          NOTES           "
    echo "##########################"
    echo "  FungiFunk_refdb uses NCBI Datasets as the protein-sequence source."
    echo ""
    echo "  FungiFunk_refdb does not predict proteins that are absent from an NCBI assembly."
    echo ""
    echo "  Species may remain in the species master even when no usable NCBI protein FASTA"
    echo "  is currently available."
    echo ""
    echo "  Multiple eligible assemblies/strains may contribute proteins to one species."
    echo ""
    echo "  Exact duplicate proteins are stored once in the final FASTA, while all original"
    echo "  species, assembly, strain and protein origins are retained in provenance metadata."
    echo ""
    echo "  Every completed reference also contains:"
    echo "    metadata/reproduction_targets.tsv"
    echo "  This records the exact versioned assemblies selected into the final reference"
    echo "  using the same schema accepted by --include-explicit-targets."
    echo ""
    echo "  Taxonomic Tier is metadata and does not create separate reference databases."
    echo ""
    echo "  Custom filters are optional. No numerical N50/BUSCO/genome-size threshold is"
    echo "  imposed unless explicitly requested."
    echo ""
    echo "  FungiFunk_refdb never intentionally removes unrelated user files."
    echo ""
    echo "  FungiFunk_refdb also never automatically removes incomplete build directories."
    echo "  If a run fails before a resumable checkpoint exists, cleanup is deliberately"
    echo "  left to the user before starting again with --create."
    echo ""

}


########################################################################################################
# Define default values for arguments
########################################################################################################
RUN_MODE=""
RUN_MODE_COUNT=0

OUTDIR=""
SOURCE_REFDB=""
SEED_TAXA=""
EXCLUDE_TAXA=""
METADATA_FILTERS=""
INCLUDE_EXPLICIT_TARGETS=""
EXCLUDE_EXPLICIT_TARGETS=""
TMPDIR_ARG=""
DB_VERSION_LABEL=""

RESET_BIOLOGICAL_CONFIG=false

WORKERS="auto"
WORKERS_EXPLICIT=false
FORCE=false

RESUME_LOCKED_OPTIONS_SEEN=()
CURATION_INPUT_OPTIONS_SEEN=()

set_run_mode() {
    local mode="$1"
    RUN_MODE_COUNT=$((RUN_MODE_COUNT + 1))
    if [[ "$RUN_MODE_COUNT" -gt 1 ]]; then
        log_msg "[ERROR] Run modes are mutually exclusive. Specify exactly one of --create, --resume, --create-new-updated-version, or --overwrite-with-updated-version." >&2
        exit 2
    fi
    RUN_MODE="$mode"
}

record_resume_locked_option() {
    RESUME_LOCKED_OPTIONS_SEEN+=("$1")
}


########################################################################################################
# Filesystem helpers
########################################################################################################
require_value() {
    local option="$1"
    local remaining="$2"
    if [[ "$remaining" -lt 2 ]]; then
        log_msg "[ERROR] ${option} requires a value." >&2
        exit 2
    fi
}

is_dir_empty() {
    local dir="$1"
    [[ -d "$dir" ]] || return 0
    [[ -z "$(find "$dir" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]
}

is_refdb_dir() {
    local dir="$1"
    [[ -d "$dir" && -f "$dir/$REFDB_MARKER_REL" ]]
}

is_resumable_refdb_dir() {
    local dir="$1"

    [[ -d "$dir" &&
       -f "$dir/$REFDB_MARKER_REL" &&
       -f "$dir/$RESUME_READY_REL" ]]
}


is_completed_refdb_dir() {
    local dir="$1"

    [[ -d "$dir" &&
       -f "$dir/$REFDB_MARKER_REL" &&
       -f "$dir/$RESUME_READY_REL" &&
       -f "$dir/$COMPLETION_MARKER_REL" ]]
}

canonicalize_path() {
    # Python is already a required dependency and provides reliable path
    # normalization even when the final path does not exist yet.
    python3 - "$1" <<'PY'
import os
import sys
print(os.path.realpath(os.path.abspath(os.path.expanduser(sys.argv[1]))))
PY
}

write_refdb_marker_if_missing() {
    local outdir="$1"
    local marker="$outdir/$REFDB_MARKER_REL"

    if [[ ! -e "$marker" ]]; then
        umask 022
        cat > "$marker" <<EOF_MARKER
Tool=${TOOL_NAME}
Version=${VERSION}
Initialized=$(date '+%Y-%m-%d %H:%M:%S')
EOF_MARKER
    fi
}

prepare_new_target_dir() {
    local outdir="$1"
    local mode="$2"

    if [[ -e "$outdir" && ! -d "$outdir" ]]; then
        log_msg "[ERROR] The specified output path exists but is not a directory: $outdir" >&2
        exit 2
    fi

    if [[ -d "$outdir" ]] && ! is_dir_empty "$outdir"; then

        if is_refdb_dir "$outdir"; then

            if is_completed_refdb_dir "$outdir"; then

                cat >&2 <<EOF_COMPLETE

[ERROR] A completed FungiFunk_refdb reference already exists in the specified
output directory:

    $outdir

${mode} starts a NEW build and will not overwrite an existing completed
reference.

To preserve this reference and create an updated copy, use
--create-new-updated-version with a different --outdir.

To safely replace this completed reference after building and validating an
updated staging reference, use --overwrite-with-updated-version.

FungiFunk_refdb will not remove or overwrite the completed reference
automatically.

EOF_COMPLETE

                exit 2
            fi

            if is_resumable_refdb_dir "$outdir"; then

                cat >&2 <<EOF_RESUMABLE

[ERROR] An incomplete but resumable FungiFunk_refdb build already exists in the
specified output directory:

    $outdir

${mode} starts a NEW build and therefore cannot reuse this directory.

Starting again with ${mode} is not necessary. Continue the existing build with:

    FungiFunk_refdb.sh --resume --outdir $outdir

Biological configuration may still be editable if assembly
search and selection has not started. Python will determine this from the
persistent build state.

FungiFunk_refdb will not remove the existing build automatically.

Recovery summary:
  Resumable state available: yes
  Biological configuration editable: determined from persistent build state
  Recommended action: run --resume for the existing build

EOF_RESUMABLE

                exit 2
            fi

            cat >&2 <<EOF_NOT_RESUMABLE

[ERROR] An incomplete FungiFunk_refdb directory already exists in the specified
output location:

    $outdir

The previous run stopped before a resumable FungiFunk_refdb state was created.

--resume cannot be used for this run because no persistent recovery checkpoint
is available yet.

Correct the problem that stopped the previous run, remove the incomplete output
directory (or its contents) manually, and start the build again with ${mode}.

FungiFunk_refdb will not remove the incomplete directory automatically.

Recovery summary:
  Resumable state available: no
  Biological configuration editable: not applicable
  Assembly search/selection started: no committed resumable state available
  Recommended action: clean the incomplete output manually and start again

EOF_NOT_RESUMABLE

            exit 2
        fi

        cat >&2 <<EOF_UNRELATED

[ERROR] The specified output directory is not empty and does not contain a
recognized FungiFunk_refdb build/database:

    $outdir

FungiFunk_refdb will not modify or remove unrelated user files.

Please select a new or empty output directory.

EOF_UNRELATED

        exit 2
    fi

    mkdir -p -- "$outdir/logs" "$outdir/state"
    write_refdb_marker_if_missing "$outdir"
}

require_existing_refdb() {
    local dir="$1"
    local purpose="$2"

    if [[ ! -e "$dir" ]]; then
        log_msg "[ERROR] ${purpose} directory does not exist: $dir" >&2
        exit 2
    fi
    if [[ ! -d "$dir" ]]; then
        log_msg "[ERROR] ${purpose} path is not a directory: $dir" >&2
        exit 2
    fi
    if ! is_refdb_dir "$dir"; then
        cat >&2 <<EOF_NOT_REFDB
[ERROR] ${purpose} is not a recognized FungiFunk_refdb directory:

    $dir

FungiFunk_refdb will not assume ownership of an unrelated directory.
EOF_NOT_REFDB
        exit 2
    fi
}

require_resumable_refdb() {
    local dir="$1"
    local purpose="$2"

    require_existing_refdb "$dir" "$purpose"

    if [[ ! -f "$dir/$RESUME_READY_REL" ]]; then

        cat >&2 <<EOF_NOT_RESUMABLE

[ERROR] ${purpose} is a recognized FungiFunk_refdb directory, but the previous
run stopped before a resumable state was created:

    $dir

--resume cannot be used because the initial persistent recovery checkpoint was
not completed.

Correct the problem that stopped the previous run, remove the incomplete output
directory (or its contents) manually, and start the build again with --create.

FungiFunk_refdb will not remove the incomplete directory automatically.

Recovery summary:
  Resumable state available: no
  Biological configuration editable: not applicable
  Assembly search/selection started: no committed resumable state available
  Recommended action: clean the incomplete output manually and run --create again

EOF_NOT_RESUMABLE

        exit 2
    fi
}

require_completed_refdb_skeleton() {

    # Check the basic filesystem contract of a successfully completed reference.
    #
    # Python performs the authoritative SHA-256 integrity validation. The shell
    # only requires the files that distinguish a completed reference from an
    # interrupted/resumable build.
    local dir="$1"
    local purpose="$2"

    require_existing_refdb "$dir" "$purpose"

    if [[ ! -f "$dir/$RESUME_READY_REL" ||
            ! -f "$dir/$FINAL_FASTA_REL" ||
            ! -f "$dir/$BUILD_MANIFEST_REL" ||
            ! -f "$dir/$COMPLETION_MARKER_REL" ]]; then

        cat >&2 <<EOF_INCOMPLETE

[ERROR] ${purpose} is recognized as FungiFunk_refdb output but does not contain
the complete filesystem checkpoint expected from a successfully finalized
reference:

    $dir/$RESUME_READY_REL
    $dir/$FINAL_FASTA_REL
    $dir/$BUILD_MANIFEST_REL
    $dir/$COMPLETION_MARKER_REL

A build manifest alone does not prove that final integrity verification
completed successfully. The completion marker is written only after the final
manifest has been verified.

If this is an interrupted build and a resumable state is available, use
--resume instead of an update mode.

EOF_INCOMPLETE

        exit 2
    fi
}

check_readable_file() {
    local option="$1"
    local path="$2"
    if [[ ! -f "$path" ]]; then
        log_msg "[ERROR] ${option} file does not exist or is not a regular file: $path" >&2
        exit 2
    fi

    if [[ ! -r "$path" ]]; then
        log_msg "[ERROR] ${option} file is not readable: $path" >&2
        exit 2
    fi
    }


########################################################################################################
# Parse arguments
########################################################################################################

if [[ "$#" -eq 0 ]]; then
    Help
    exit 2
fi

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        -h|--help)
            Help
            exit 0
            ;;
        --version)
            echo "${TOOL_NAME} v${VERSION}"
            exit 0
            ;;

        --create)
            set_run_mode "create"
            shift
            ;;
        --resume)
            set_run_mode "resume"
            shift
            ;;
        --create-new-updated-version)
            set_run_mode "create-new-updated-version"
            shift
            ;;
        --overwrite-with-updated-version)
            set_run_mode "overwrite-with-updated-version"
            shift
            ;;

        --retry-ncbi-failures)
            set_run_mode "retry-ncbi-failures"
            shift
            ;;

        -o|--outdir)
            require_value "$1" "$#"
            OUTDIR="$2"
            shift 2
            ;;
        --source-refdb)
            require_value "$1" "$#"
            SOURCE_REFDB="$2"
            shift 2
            ;;
        --seed-taxa)
            require_value "$1" "$#"
            SEED_TAXA="$2"
            CURATION_INPUT_OPTIONS_SEEN+=("--seed-taxa")
            shift 2
            ;;
        --exclude-taxa)
            require_value "$1" "$#"
            EXCLUDE_TAXA="$2"
            CURATION_INPUT_OPTIONS_SEEN+=("--exclude-taxa")
            shift 2
            ;;
        --metadata-filters)
            require_value "$1" "$#"
            METADATA_FILTERS="$2"
            CURATION_INPUT_OPTIONS_SEEN+=("--metadata-filters")
            shift 2
            ;;
        --include-explicit-targets)
            require_value "$1" "$#"
            INCLUDE_EXPLICIT_TARGETS="$2"
            CURATION_INPUT_OPTIONS_SEEN+=("--include-explicit-targets")
            shift 2
            ;;
        --exclude-explicit-targets)
            require_value "$1" "$#"
            EXCLUDE_EXPLICIT_TARGETS="$2"
            CURATION_INPUT_OPTIONS_SEEN+=("--exclude-explicit-targets")
            shift 2
            ;;
        --tmpdir)
            require_value "$1" "$#"
            TMPDIR_ARG="$2"
            shift 2
            ;;
        --db-version)
            require_value "$1" "$#"
            DB_VERSION_LABEL="$2"
            record_resume_locked_option "--db-version"
            shift 2
            ;;
        --reset-biological-config)
            RESET_BIOLOGICAL_CONFIG=true
            shift
            ;;
        --workers)
            require_value "$1" "$#"
            WORKERS="$2"
            WORKERS_EXPLICIT=true
            shift 2
            ;;
        --force)
            FORCE=true
            shift
            ;;
        --verbose|--timestamps)
            # Runtime presentation options are accepted by the wrapper and
            # resolved by the Python script.
            shift
            ;;
        --include-gca|--include-isolates|--include-unassigned-assemblies|--include-atypical|--require-busco)
            # Boolean biological options are accepted by the wrapper and
            # resolved by the Python script.
            shift
            ;;
        --min-contig-n50|--max-contig-l50|--max-contigs|\
        --min-scaffold-n50|--max-scaffold-l50|--max-scaffolds|--max-scaffold-gaps|\
        --min-genome-size|--max-genome-size|\
        --min-gc-percent|--max-gc-percent|\
        --min-protein-coding-genes|--max-protein-coding-genes|\
        --min-busco-complete|--max-busco-missing|--max-busco-fragmented|--max-busco-duplicated|\
        --assembly-level|--released-after|--released-before|--refseq-category|\
        --assembly-method-contains|--sequencing-technology-contains)
            require_value "$1" "$#"
            shift 2
            ;;
        --max-assemblies-per-species|--max-assemblies-per-strain|--max-strains-per-species)
            require_value "$1" "$#"
            shift 2
            ;;
        --)
            # This wrapper has no positional arguments. Treat anything after --
            # as invalid rather than silently forwarding an ambiguous token set.
            shift
            if [[ "$#" -gt 0 ]]; then
                log_msg "[ERROR] Unexpected positional argument(s) after --: $*" >&2
                exit 2
            fi
            ;;

        -* )
            log_msg "[ERROR] Unknown option: $1. Run --help to see the supported interface." >&2
            exit 2
            ;;
        *)
            log_msg "[ERROR] Unexpected positional argument: $1. Run --help for usage." >&2
            exit 2
            ;;
    esac
done


########################################################################################################
# Validate arguments
########################################################################################################
if [[ "$RUN_MODE_COUNT" -eq 0 ]]; then
    log_msg "[ERROR] No run mode was specified. Choose exactly one of --create, --resume, --create-new-updated-version, --overwrite-with-updated-version, or --retry-ncbi-failures."
    Help
    exit 2
fi

if [[ "$RUN_MODE" == "retry-ncbi-failures" ]]; then

    if [[ -n "$OUTDIR" ]]; then
        log_msg "[ERROR] --retry-ncbi-failures uses --source-refdb; --outdir must not be supplied." >&2
        exit 2
    fi

    if [[ -z "$SOURCE_REFDB" ]]; then
        log_msg "[ERROR] --retry-ncbi-failures requires --source-refdb DIR." >&2
        exit 2
    fi

    # Reuse the source path internally for wrapper path validation and logging.
    # The original CLI arguments are forwarded unchanged to Python.
    OUTDIR="$SOURCE_REFDB"

elif [[ -z "$OUTDIR" ]]; then

    log_msg "[ERROR] --outdir is required with --${RUN_MODE}." >&2
    exit 2

fi


if [[ "$WORKERS" != "auto" && ! "$WORKERS" =~ ^[1-9][0-9]*$ ]]; then
    log_msg "[ERROR] --workers must be 'auto' or a positive integer (received: $WORKERS)." >&2
    exit 2
fi

if [[ "$FORCE" == true ]]; then
    if [[ "$WORKERS_EXPLICIT" != true || "$WORKERS" == "auto" ]]; then
        log_msg "[ERROR] --force only applies to an explicit numeric --workers N request. Example: --workers 16 --force" >&2
        exit 2
    fi
fi

# Biological configuration reset is valid for update modes and for
# pre-assembly --resume. Python is the state-aware validator
# for whether a resume is still early enough to change biological settings.
if [[ "$RESET_BIOLOGICAL_CONFIG" == true &&
      "$RUN_MODE" != "create-new-updated-version" &&
      "$RUN_MODE" != "overwrite-with-updated-version" &&
      "$RUN_MODE" != "resume" ]]; then

    log_msg "[ERROR] --reset-biological-config is not valid with --create because --create already starts from clean biological defaults." >&2
    exit 2
fi


########################################################################################################
# Validate dependencies before creating/modifying target output structures
########################################################################################################
if ! command -v python3 >/dev/null 2>&1; then
    log_msg "[ERROR] Required dependency 'python3' was not found in PATH." >&2
    exit 2
fi

if ! command -v datasets >/dev/null 2>&1; then
    log_msg "[ERROR] Required NCBI Datasets CLI executable 'datasets' was not found in PATH." >&2
    exit 2
fi

if [[ ! -f "$PYTHON_SCRIPT" || ! -r "$PYTHON_SCRIPT" ]]; then
    log_msg "[ERROR] Core Python implementation was not found or is not readable: $PYTHON_SCRIPT" >&2
    exit 2
fi

# Canonical paths are used only for safety comparisons/logging; the original
# user arguments are still forwarded unchanged to Python.
OUTDIR_CANON="$(canonicalize_path "$OUTDIR")"
SOURCE_REFDB_CANON=""
if [[ -n "$SOURCE_REFDB" ]]; then
    SOURCE_REFDB_CANON="$(canonicalize_path "$SOURCE_REFDB")"
fi

# Validate any user-supplied input files before target initialization.
[[ -z "$SEED_TAXA" ]] || check_readable_file "--seed-taxa" "$SEED_TAXA"
[[ -z "$EXCLUDE_TAXA" ]] || check_readable_file "--exclude-taxa" "$EXCLUDE_TAXA"
[[ -z "$METADATA_FILTERS" ]] || check_readable_file "--metadata-filters" "$METADATA_FILTERS"
[[ -z "$INCLUDE_EXPLICIT_TARGETS" ]] || check_readable_file "--include-explicit-targets" "$INCLUDE_EXPLICIT_TARGETS"
[[ -z "$EXCLUDE_EXPLICIT_TARGETS" ]] || check_readable_file "--exclude-explicit-targets" "$EXCLUDE_EXPLICIT_TARGETS"


########################################################################################################
# Validate run-mode and filesystem safety
########################################################################################################
case "$RUN_MODE" in
    create)
        if [[ -n "$SOURCE_REFDB" ]]; then
            log_msg "[ERROR] --source-refdb is not valid with --create." >&2
            exit 2
        fi

        if [[ -z "$SEED_TAXA" && -z "$INCLUDE_EXPLICIT_TARGETS" ]]; then
            log_msg "[ERROR] --create requires at least one of --seed-taxa FILE or --include-explicit-targets FILE." >&2
            exit 2
        fi

        prepare_new_target_dir "$OUTDIR_CANON" "--create"
        ;;

    resume)
        if [[ -n "$SOURCE_REFDB" ]]; then
            log_msg "[ERROR] --source-refdb is not valid with --resume." >&2
            exit 2
        fi

        # Build identity/provenance options are locked during every resume.
        # Biological options are NOT rejected here because
        # Python determines whether they are editable from the persistent build
        # state.
        if [[ "${#RESUME_LOCKED_OPTIONS_SEEN[@]}" -gt 0 ]]; then

            log_msg '[ERROR] --resume cannot change build identity/provenance options.\n' >&2
            printf 'The following locked option(s) were supplied:\n' >&2
            printf '  %s\n' "${RESUME_LOCKED_OPTIONS_SEEN[@]}" >&2
            printf '\n' >&2
            printf 'The database version belongs to the identity of this existing build and\n' >&2
            printf 'cannot be changed during resume.\n' >&2
            printf '\n' >&2
            printf 'To continue this build, rerun --resume without the locked option(s).\n' >&2

            exit 2
        fi

        require_resumable_refdb "$OUTDIR_CANON" "--resume target"

        # Biological options, including corrected TSV inputs
        # and --reset-biological-config, are passed through to Python. Python accepts
        # them only before ASSEMBLY_SEARCH_AND_SELECTION has started.
        ;;

    create-new-updated-version)
        if [[ -z "$SOURCE_REFDB" ]]; then
            log_msg "[ERROR] --create-new-updated-version requires --source-refdb DIR." >&2
            exit 2
        fi

        if [[ "$SOURCE_REFDB_CANON" == "$OUTDIR_CANON" ]]; then
            log_msg "[ERROR] --source-refdb and --outdir must be different with --create-new-updated-version." >&2
            exit 2
        fi

        require_completed_refdb_skeleton "$SOURCE_REFDB_CANON" "--source-refdb"

        if [[ "$RESET_BIOLOGICAL_CONFIG" == true ]]; then
            if [[ -z "$SEED_TAXA" && -z "$INCLUDE_EXPLICIT_TARGETS" ]]; then
                log_msg "[ERROR] --reset-biological-config requires at least one of --seed-taxa FILE or --include-explicit-targets FILE." >&2
                exit 2
            fi
        fi

        prepare_new_target_dir "$OUTDIR_CANON" "--create-new-updated-version"
        ;;

    overwrite-with-updated-version)
        if [[ -n "$SOURCE_REFDB" ]]; then
            log_msg "[ERROR] --source-refdb is not used with --overwrite-with-updated-version; the existing --outdir is both source and replacement target." >&2
            exit 2
        fi

        require_completed_refdb_skeleton "$OUTDIR_CANON" "--overwrite-with-updated-version target"

        if [[ "$RESET_BIOLOGICAL_CONFIG" == true ]]; then
            if [[ -z "$SEED_TAXA" && -z "$INCLUDE_EXPLICIT_TARGETS" ]]; then
                log_msg "[ERROR] --reset-biological-config requires at least one of --seed-taxa FILE or --include-explicit-targets FILE." >&2
                exit 2
            fi
        fi
        ;;

    retry-ncbi-failures)
        require_completed_refdb_skeleton "$SOURCE_REFDB_CANON" "--source-refdb"

        recovery_parent="$(dirname -- "$SOURCE_REFDB_CANON")"
        recovery_name="$(basename -- "$SOURCE_REFDB_CANON")"

        recovery_staging="${recovery_parent}/.${recovery_name}.FungiFunk_refdb_recovery_staging"
        recovery_backup="${recovery_parent}/.${recovery_name}.FungiFunk_refdb_previous_backup"

        if [[ -e "$recovery_staging" || -L "$recovery_staging" ]]; then
            log_msg "[ERROR] A previous manual NCBI recovery staging path already exists:" >&2
            log_msg "        $recovery_staging" >&2
            log_msg "        Check that no recovery process is still running." >&2
            log_msg "        Inspect and remove the incomplete staging directory manually before retrying." >&2
            exit 2
        fi

        if [[ -e "$recovery_backup" || -L "$recovery_backup" ]]; then
            log_msg "[ERROR] A previous safe-overwrite backup exists:" >&2
            log_msg "        $recovery_backup" >&2
            log_msg "        Inspect this backup before starting manual NCBI recovery." >&2
            log_msg "        FungiFunk_refdb will not delete it automatically." >&2
            exit 2
        fi
        ;;

    *)
        log_msg "[ERROR] Internal wrapper error: unsupported run mode '$RUN_MODE'." >&2
        exit 2
        ;;
esac

# If the user provided --tmpdir, make sure the path is a writable directory.
if [[ -n "$TMPDIR_ARG" ]]; then
    if [[ -e "$TMPDIR_ARG" && ! -d "$TMPDIR_ARG" ]]; then
        log_msg "[ERROR] --tmpdir exists but is not a directory: $TMPDIR_ARG" >&2
        exit 2
    fi
    mkdir -p -- "$TMPDIR_ARG"
    if [[ ! -w "$TMPDIR_ARG" ]]; then
        log_msg "[ERROR] --tmpdir is not writable: $TMPDIR_ARG" >&2
        exit 2
    fi
fi

# Detect whether --resume is operating on the deterministic staging directory
# created by a previous --overwrite-with-updated-version invocation.
RESUMING_OVERWRITE="false"
RESUMED_OVERWRITE_TARGET=""

if [[ "$RUN_MODE" == "resume" ]]; then

    resume_dir_name="$(basename -- "$OUTDIR_CANON")"
    resume_parent="$(dirname -- "$OUTDIR_CANON")"

    if [[ -f "${OUTDIR_CANON}/${REFDB_MARKER_REL}" &&
          "$resume_dir_name" == .* &&
          "$resume_dir_name" == *".FungiFunk_refdb_update_staging" ]]; then

        # Remove the leading dot and the fixed staging suffix to reconstruct the
        # original reference directory name.
        resume_target_name="${resume_dir_name#.}"
        resume_target_name="${resume_target_name%.FungiFunk_refdb_update_staging}"

        if [[ -n "$resume_target_name" ]]; then

            RESUMING_OVERWRITE="true"
            RESUMED_OVERWRITE_TARGET="${resume_parent}/${resume_target_name}"

        fi

    fi

fi


########################################################################################################
# Prepare log file and write run metadata
########################################################################################################

# Define the run metadata log file.
# Staged replacement modes must not modify the original reference.
if [[ "$RUN_MODE" == "overwrite-with-updated-version" ||
      "$RUN_MODE" == "retry-ncbi-failures" ]]; then

    # Write the run metadata log to a temporary file beside the existing database
    # so the old reference is not modified during the staged update
    overwrite_parent="$(dirname -- "$OUTDIR_CANON")"
    overwrite_name="$(basename -- "$OUTDIR_CANON")"

    run_info_log="$(mktemp "${overwrite_parent}/.${overwrite_name}.FungiFunk_refdb_wrapper.XXXXXX")"

else

    run_info_log="${OUTDIR_CANON}/${BASIC_LOG_REL}"

    # Create the log directory before writing the run metadata
    mkdir -p -- "$(dirname -- "$run_info_log")"

fi

# Detect whether an NCBI API key is available. The key itself is never written to the log.
if [[ -n "${NCBI_API_KEY:-}" ]]; then
    NCBI_API_KEY_DETECTED="yes"
else
    NCBI_API_KEY_DETECTED="no"
fi

# Record software versions used for this invocation
PYTHON_VERSION="$(python3 --version 2>&1 || true)"
DATASETS_VERSION="$(datasets version 2>/dev/null || datasets --version 2>/dev/null || echo "unknown")"

# Record whether this invocation explicitly requested a clean biological
# configuration rather than inheriting the existing one.
if [[ "$RUN_MODE" == "create-new-updated-version" ||
      "$RUN_MODE" == "overwrite-with-updated-version" ||
      "$RUN_MODE" == "resume" ]]; then

    if [[ "$RESET_BIOLOGICAL_CONFIG" == true ]]; then
        BIOLOGICAL_CONFIG_RESET_LOG="yes"
    else
        BIOLOGICAL_CONFIG_RESET_LOG="no"
    fi

else
    BIOLOGICAL_CONFIG_RESET_LOG="not used"
fi

# Write run metadata to log file
cat >> "$run_info_log" <<EOF
Tool: ${TOOL_NAME}
Version: ${VERSION}
Run date: $(date '+%Y-%m-%d %H:%M:%S')
Working directory: $(pwd)
Command: ${COMMAND_RUN}

Run mode:
  mode: ${RUN_MODE}
  biological_config_reset: ${BIOLOGICAL_CONFIG_RESET_LOG}

Input:
  seed_taxa: ${SEED_TAXA:-not supplied}
  exclude_taxa: ${EXCLUDE_TAXA:-not supplied}
  metadata_filters: ${METADATA_FILTERS:-not supplied}
  source_refdb: ${SOURCE_REFDB_CANON:-not used}

Output:
  outdir: ${OUTDIR_CANON}
  tmpdir: ${TMPDIR_ARG:-default}
  final_fasta: ${FINAL_FASTA_NAME}
  db_version: ${DB_VERSION_LABEL:-pending resolution}

Resources:
  workers: ${WORKERS}
  force: ${FORCE}

NCBI:
  API_key_detected: ${NCBI_API_KEY_DETECTED}

Software:
  Python_executable: $(command -v python3)
  Python_version: ${PYTHON_VERSION}
  NCBI_Datasets_executable: $(command -v datasets)
  NCBI_Datasets_version: ${DATASETS_VERSION}
  Python_module: ${PYTHON_SCRIPT}

EOF


########################################################################################################
# Print run start messages
########################################################################################################
echo "###################################################################################################"
echo " _____                  _ _____            _                  __     _ _          "
echo "|  ___|   _ _ __   __ _(_)  ___|   _ _ __ | | __    _ __ ___ / _| __| | |__       "
echo "| |_ | | | |  _ \ / _  | | |_ | | | |  _ \| |/ /   |  __/ _ \ |_ / _  | '_ \      "
echo "|  _|| |_| | | | | (_| | |  _|| |_| | | | |   <    | | |  __/  _| (_| | |_) |     "
echo "|_|   \__,_|_| |_|\__, |_|_|   \__,_|_| |_|_|\_\___|_|  \___|_|  \__,_|_.__/      "
echo "                  |___/                                                           "
echo ""
echo "Running ${TOOL_NAME} ${VERSION}"
echo "Run mode: ${RUN_MODE}"
echo "Output directory: ${OUTDIR_CANON}"
echo "Run information log: ${run_info_log}"

if [[ "$RUN_MODE" == "create-new-updated-version" ]]; then
    echo "Source reference: ${SOURCE_REFDB_CANON}"
elif [[ "$RUN_MODE" == "overwrite-with-updated-version" ]]; then
    echo "[WARNING] Safe overwrite update selected. The existing database will remain valid until the staged update passes final validation." >&2
    echo "[WARNING] Temporary disk usage may approach the size of the existing database plus normal working-space requirements." >&2
fi

if [[ "$RUN_MODE" == "create-new-updated-version" ||
      "$RUN_MODE" == "overwrite-with-updated-version" ]]; then

    if [[ "$RESET_BIOLOGICAL_CONFIG" == true ]]; then
        echo "Biological configuration: reset to clean defaults before applying explicit options"
    else
        echo "Biological configuration: inherit previous settings, then apply explicit overrides"
    fi

fi

echo ""


########################################################################################################
# Run Python script
########################################################################################################

# Run the Python implementation and save its exit status for final logging.
# Temporarily disable 'set -e' so the wrapper can continue if Python fails.
set +e

FUNGIFUNK_WRAPPER_RUN_INFO_LOG="$run_info_log" \
    python3 "$PYTHON_SCRIPT" "${ORIGINAL_ARGS[@]}"

PYTHON_STATUS=$?
set -e

# Manual recovery can complete successfully without publishing changes.
# Require an unambiguous receipt from the Python process.
RECOVERY_ACTION=""

if [[ "$RUN_MODE" == "retry-ncbi-failures" &&
      "$PYTHON_STATUS" -eq 0 ]]; then

    if grep -Fxq 'Manual_recovery_action: PROMOTE' "$run_info_log" &&
       ! grep -Fxq 'Manual_recovery_action: NO_CHANGE' "$run_info_log"; then

        RECOVERY_ACTION="PROMOTE"

    elif grep -Fxq 'Manual_recovery_action: NO_CHANGE' "$run_info_log" &&
         ! grep -Fxq 'Manual_recovery_action: PROMOTE' "$run_info_log"; then

        RECOVERY_ACTION="NO_CHANGE"

    else

        log_msg "[ERROR] Manual NCBI recovery returned no unambiguous publication status."
        log_msg "[ERROR] Refusing to append recovery logs to the reference."
        PYTHON_STATUS=1

    fi

fi


########################################################################################################
# Finalise run metadata log and run status
########################################################################################################

# Define the final status of this run
if [[ "$PYTHON_STATUS" -eq 0 ]]; then
    RUN_RESULT="SUCCESS"
else
    RUN_RESULT="FAILED"
fi


########################################################################################################
# Relocate and finalize the run metadata log after overwrite operations
########################################################################################################

if [[ "$RUN_MODE" == "overwrite-with-updated-version" ||
      "$RUN_MODE" == "retry-ncbi-failures" ]]; then

    overwrite_parent="$(dirname -- "$OUTDIR_CANON")"
    overwrite_name="$(basename -- "$OUTDIR_CANON")"

    # Keep staging names consistent with the Python implementation.
    if [[ "$RUN_MODE" == "retry-ncbi-failures" ]]; then
        staging_dir="${overwrite_parent}/.${overwrite_name}.FungiFunk_refdb_recovery_staging"
    else
        staging_dir="${overwrite_parent}/.${overwrite_name}.FungiFunk_refdb_update_staging"
    fi

    if [[ "$PYTHON_STATUS" -eq 0 &&
          "$RUN_MODE" == "retry-ncbi-failures" &&
          "$RECOVERY_ACTION" == "NO_CHANGE" ]]; then

        # Python completed without publishing anything. Do not write logs
        # into the unchanged original reference.
        unchanged_run_info_log="${overwrite_parent}/.${overwrite_name}.FungiFunk_refdb_recovery_no_changes.log"

        if cat -- "$run_info_log" >> "$unchanged_run_info_log"; then

            rm -f -- "$run_info_log"
            run_info_log="$unchanged_run_info_log"

        else

            log_msg "[ERROR] Could not preserve the no-change recovery log."
            log_msg "[ERROR] Temporary run metadata log preserved at: ${run_info_log}"

        fi

    elif [[ "$PYTHON_STATUS" -eq 0 ]]; then

        # Python has completed and validated the staged reference and has already
        # promoted it to OUTDIR_CANON.
        #
        # OUTDIR_CANON therefore now points to the NEW valid reference database.
        # It is safe to append the temporary run metadata log to its permanent log.
        final_run_info_log="${OUTDIR_CANON}/${BASIC_LOG_REL}"

        mkdir -p -- "$(dirname -- "$final_run_info_log")"

        if cat -- "$run_info_log" >> "$final_run_info_log"; then

            rm -f -- "$run_info_log"
            run_info_log="$final_run_info_log"

        else

            log_msg "[ERROR] Could not append the run metadata log to the updated reference database."
            log_msg "[ERROR] Temporary run metadata log preserved at: ${run_info_log}"

        fi

    elif [[ -d "$staging_dir" ]]; then

        # Python failed after creating the staging database.
        #
        # Preserve the invocation log inside the staging build so it remains
        # available for inspection and a later --resume.
        #
        # The original valid reference database remains untouched.
        staging_run_info_log="${staging_dir}/${BASIC_LOG_REL}"

        mkdir -p -- "$(dirname -- "$staging_run_info_log")"

        if cat -- "$run_info_log" >> "$staging_run_info_log"; then

            rm -f -- "$run_info_log"
            run_info_log="$staging_run_info_log"

        else

            log_msg "[ERROR] Could not append the run metadata log to the staging database."
            log_msg "[ERROR] Temporary run metadata log preserved at: ${run_info_log}"

        fi

    else

        # Python failed before a staging database was created.
        #
        # Keep the failure log beside the reference rather than writing anything
        # inside the still-valid original database.
        if [[ "$RUN_MODE" == "retry-ncbi-failures" ]]; then
            failed_run_info_log="${overwrite_parent}/.${overwrite_name}.FungiFunk_refdb_recovery_failed.log"
        else
            failed_run_info_log="${overwrite_parent}/.${overwrite_name}.FungiFunk_refdb_update_failed.log"
        fi

        if cat -- "$run_info_log" >> "$failed_run_info_log"; then

            rm -f -- "$run_info_log"
            run_info_log="$failed_run_info_log"

        else

            log_msg "[ERROR] Could not preserve the overwrite failure log."
            log_msg "[ERROR] Temporary run metadata log preserved at: ${run_info_log}"

        fi

    fi

elif [[ "$RUN_MODE" == "resume" &&
        "$RESUMING_OVERWRITE" == "true" &&
        "$PYTHON_STATUS" -eq 0 &&
        ! -d "$OUTDIR_CANON" &&
        -d "$RESUMED_OVERWRITE_TARGET" ]]; then

    # This run resumed a staging database originally created by
    # --overwrite-with-updated-version.
    #
    # Python has successfully completed validation and moved the entire staging
    # directory to the original reference path. build_info.log moved with that
    # directory, so update the shell variable before appending completion data.
    run_info_log="${RESUMED_OVERWRITE_TARGET}/${BASIC_LOG_REL}"

fi


########################################################################################################
# Append completion information to the final/current log
########################################################################################################

cat >> "$run_info_log" <<EOF
Run completion:
  Python_exit_status: ${PYTHON_STATUS}
  Result: ${RUN_RESULT}
  End date: $(date '+%Y-%m-%d %H:%M:%S')

EOF


########################################################################################################
# Print a final message for the user
########################################################################################################

if [[ "$PYTHON_STATUS" -eq 0 ]]; then

    log_msg "[INFO]   Run log: ${run_info_log}"
    echo ""
    echo "----------"
    log_msg "${TOOL_NAME} completed successfully!"
    echo "----------"
    echo ""

else

    log_msg "[ERROR] ${TOOL_NAME} stopped with exit status ${PYTHON_STATUS}."
    log_msg "[ERROR] Run log: ${run_info_log}"

    if [[ "$RUN_MODE" == "overwrite-with-updated-version" ]]; then
        log_msg "[ERROR] The previous valid reference database was not replaced."
    fi

fi

exit "$PYTHON_STATUS"