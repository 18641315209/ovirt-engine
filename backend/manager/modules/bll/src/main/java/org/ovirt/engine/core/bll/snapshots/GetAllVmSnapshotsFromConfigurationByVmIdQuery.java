package org.ovirt.engine.core.bll.snapshots;

import java.util.List;

import javax.inject.Inject;

import org.ovirt.engine.core.bll.QueriesCommandBase;
import org.ovirt.engine.core.bll.context.EngineContext;
import org.ovirt.engine.core.common.businessentities.Snapshot;
import org.ovirt.engine.core.common.businessentities.VM;
import org.ovirt.engine.core.common.queries.IdQueryParameters;
import org.ovirt.engine.core.dao.SnapshotDao;

/**
 * Returns a list of all snapshots for a specified VM ID including configuration data.
 */
public class GetAllVmSnapshotsFromConfigurationByVmIdQuery<P extends IdQueryParameters> extends QueriesCommandBase<P> {

    @Inject
    private SnapshotVmConfigurationHelper snapshotVmConfigurationHelper;

    @Inject
    private SnapshotDao snapshotDao;

    public GetAllVmSnapshotsFromConfigurationByVmIdQuery(P parameters, EngineContext engineContext) {
        super(parameters, engineContext);
    }

    @Override
    protected void executeQueryCommand() {
        SnapshotVmConfigurationHelper snapshotVmConfigurationHelper = getSnapshotVmConfigurationHelper();
        List<Snapshot> snapshots = snapshotDao.getAllWithConfiguration(getParameters().getId());
        for (Snapshot snapshot : snapshots) {
            VM vm = snapshotVmConfigurationHelper.getVmFromConfiguration(
                    snapshot.getVmConfiguration(), snapshot.getVmId(), snapshot.getId());
            if (vm != null) {
                snapshot.setDiskImages(vm.getImages());
            }
        }
        getQueryReturnValue().setReturnValue(snapshots);
    }

    protected SnapshotVmConfigurationHelper getSnapshotVmConfigurationHelper() {
        return snapshotVmConfigurationHelper;
    }
}
