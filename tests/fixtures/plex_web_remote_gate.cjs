// Verbatim Plex Web 4.163.0 module 24957; provenance is in plex_web_remote_gate.json.
// Only its React/Redux inputs and imported constants are adapted for a headless consumer.
'use strict';
const shippedGateFactory = (e,t,r)=>{"use strict";r.d(t,{A:()=>c});var n=r(71468),i=r(22701),s=r(19367),a=r(93764),o=r(21324),l=r(19927);const c=function(e,{type:t,subtype:r,playlistType:c}){var d,u;const p=e?(0,s.A)(e):{serverIdentifier:"",providerIdentifier:""},m=(0,n.d4)((0,o.A)(p.serverIdentifier)),h=(0,n.d4)((0,l.A)(i.b)),y=(0,n.d4)((0,l.A)(i.F_)),g=!!(null==(d=null==m?void 0:m.activeConnection)?void 0:d.isPrivate),f=r===a.i9.Movie||r===a.i9.Show||r===a.i9.Season||r===a.i9.Episode,v=c===a.zu.Video,A=t===a.QD.Movie||t===a.QD.Show||t===a.QD.Season||t===a.QD.Episode||t===a.QD.Collection&&f||t===a.QD.Playlist&&v;return!(!m||m.isCloud||g||!h||!A||y||(null==(u=m.ownerFeatures)?void 0:u.some(e=>[i.F_,i.dQ].includes(e))))}};

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
