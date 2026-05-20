async function approveUser(id) {
    if(!confirm('Are you sure you want to approve this user?')) return;
    const res = await fetch(`/admin/approve/${id}`, {method: 'POST'});
    if(res.ok) window.location.reload();
}

async function rejectUser(id) {
    if(!confirm('Are you sure you want to reject and delete this user?')) return;
    const res = await fetch(`/admin/reject/${id}`, {method: 'POST'});
    if(res.ok) window.location.reload();
}