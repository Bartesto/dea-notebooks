for file in /g/data/if87/ARD_interoperability/L2/gz/*/*tar.gz; do product=`echo $file | sed 's/.tar.gz//g'`; dest_folder=`echo $product | sed 's/gz/unzip/g'`; echo $dest_folder; if [ ! -d $dest_folder ]; then mkdir -p $dest_folder; tar -xvzf $file -C $dest_folder; fi done