## Set up config file
# Create conda environment from the tito_env.yml file.
echo "Creating conda environment from tito_env.yml..."
conda env create -f tito_env.yml 
# Activate the conda environment
conda activate tito_env2

chmod +x pipeline.sh

mkdir -p EF5_conf/precip EF5_conf/precipEF5 EF5_conf/qpf_store \
         EF5_conf/states EF5_conf/basic EF5_conf/parameters \
         EF5_conf/pet EF5_conf/templates outputs

echo "Environment installed successfully..."
