// Runs Plex Web 4.163.0 module 24957, which test_plextv.py fetches from app.plex.tv at test time
// and checks against plex_web_remote_gate.json; none of Plex's code is checked in. Only its inputs are adapted here.
'use strict';
const shippedGateFactory = (0, eval)(require('fs').readFileSync(process.env.REX_PLEX_WEB_GATE, 'utf8'));

module.exports = function ({ownerFeatures, viewerHasSubscription, isLocal}) {
    const server = {machineIdentifier: 'rex-fixture', isCloud: false,
        activeConnection: {isPrivate: isLocal}, ownerFeatures};
    const feature = {b: 'remote-watch-pass-iap', F_: 'remote-watch-pass',
        dQ: '76ddd91e-8321-4916-94b6-ded8e3727a64'};
    const videoKinds = {Movie: 'movie', Show: 'show', Season: 'season', Episode: 'episode',
        Collection: 'collection', Playlist: 'playlist'};
    const modules = {
        71468: {d4: selector => selector()},
        22701: feature,
        19367: {A: () => ({serverIdentifier: server.machineIdentifier, providerIdentifier: ''})},
        93764: {QD: videoKinds, i9: videoKinds, zu: {Video: 'video'}},
        21324: {A: () => () => server},
        // Enable the payment-gate rollout, but grant no viewer entitlement unless supplied.
        19927: {A: name => () => name === feature.b ||
            (name === feature.F_ && viewerHasSubscription)}
    };
    const load = id => {
        if (!(id in modules)) throw new Error('Unknown gate dependency ' + id);
        return modules[id];
    };
    load.d = (exports, getters) => {
        for (const key of Object.keys(getters))
            Object.defineProperty(exports, key, {get: getters[key]});
    };
    const exports = {};
    shippedGateFactory({}, exports, load);
    return !exports.A('server://rex-fixture/com.plexapp.plugins.library/library/metadata/1', {type: 'movie'});
};
