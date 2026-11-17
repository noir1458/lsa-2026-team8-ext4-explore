#!/usr/bin/env python3
"""Generate the isolated ES experiment patch from the pristine 5.4.214 file."""
from pathlib import Path
import difflib

root = Path(__file__).resolve().parents[1]
source = root / "source/fs/ext4/extents_status.c"
original = source.read_text()
text = original.replace('#include <linux/seq_file.h>', '#include <linux/seq_file.h>\n#include <linux/module.h>\n#include <linux/sched.h>')
controls = r'''
/* Experiment: lookup locality hint. On-disk state and invalidation unchanged. */
static bool es_lookup_refresh;
module_param(es_lookup_refresh, bool, 0644);
MODULE_PARM_DESC(es_lookup_refresh, "Refresh ES last-extent hint after lookup (default off)");
static bool es_lookup_profile;
module_param(es_lookup_profile, bool, 0644);
MODULE_PARM_DESC(es_lookup_profile, "Profile es_bench tasks; disable for timing");
static atomic64_t es_exp_lookups = ATOMIC64_INIT(0);
static atomic64_t es_exp_pointer_hits = ATOMIC64_INIT(0);
static atomic64_t es_exp_tree_nodes = ATOMIC64_INIT(0);
static atomic64_t es_exp_tree_hits = ATOMIC64_INIT(0);
static atomic64_t es_exp_tree_misses = ATOMIC64_INIT(0);
static atomic64_t es_exp_refreshes = ATOMIC64_INIT(0);

static int es_exp_stats_get(char *buf, const struct kernel_param *kp)
{
	return scnprintf(buf, PAGE_SIZE,
		"lookups=%lld pointer_hits=%lld tree_nodes=%lld tree_hits=%lld tree_misses=%lld refreshes=%lld\n",
		(long long)atomic64_read(&es_exp_lookups),
		(long long)atomic64_read(&es_exp_pointer_hits),
		(long long)atomic64_read(&es_exp_tree_nodes),
		(long long)atomic64_read(&es_exp_tree_hits),
		(long long)atomic64_read(&es_exp_tree_misses),
		(long long)atomic64_read(&es_exp_refreshes));
}
static const struct kernel_param_ops es_exp_stats_ops = { .get = es_exp_stats_get };
module_param_cb(es_lookup_stats, &es_exp_stats_ops, NULL, 0444);

static int es_exp_reset_set(const char *val, const struct kernel_param *kp)
{
	bool reset;
	int err = kstrtobool(val, &reset);
	if (err)
		return err;
	if (!reset)
		return -EINVAL;
	if (READ_ONCE(es_lookup_profile))
		return -EBUSY;
	atomic64_set(&es_exp_lookups, 0);
	atomic64_set(&es_exp_pointer_hits, 0);
	atomic64_set(&es_exp_tree_nodes, 0);
	atomic64_set(&es_exp_tree_hits, 0);
	atomic64_set(&es_exp_tree_misses, 0);
	atomic64_set(&es_exp_refreshes, 0);
	return 0;
}
static const struct kernel_param_ops es_exp_reset_ops = { .set = es_exp_reset_set };
module_param_cb(es_lookup_reset, &es_exp_reset_ops, NULL, 0200);

/* Replay only during profiling: no counter branch in the timed tree loop.
 * tree_nodes counts visited RB nodes, not individual comparison operators.
 * Caller holds i_es_lock, so the tree and the original hit stay valid.
 */
static void es_exp_record(struct ext4_es_tree *tree, ext4_lblk_t lblk,
			  bool pointer_hit, bool found)
{
	struct rb_node *node;
	struct extent_status *es;
	unsigned int visits = 0;
	atomic64_inc(&es_exp_lookups);
	if (pointer_hit) {
		atomic64_inc(&es_exp_pointer_hits);
		return;
	}
	node = tree->root.rb_node;
	while (node) {
		es = rb_entry(node, struct extent_status, rb_node);
		visits++;
		if (lblk < es->es_lblk)
			node = node->rb_left;
		else if (lblk - es->es_lblk >= es->es_len)
			node = node->rb_right;
		else
			break;
	}
	atomic64_add(visits, &es_exp_tree_nodes);
	if (found)
		atomic64_inc(&es_exp_tree_hits);
	else
		atomic64_inc(&es_exp_tree_misses);
}
'''
text = text.replace('static struct kmem_cache *ext4_es_cachep;', controls + '\nstatic struct kmem_cache *ext4_es_cachep;', 1)
# Common safety baseline: upstream 492888df0c7b42fc0843631168b0021bc4caee84.
old = '''\tif (tree->cache_es) {
\t\tes1 = tree->cache_es;
\t\tif (in_range(lblk, es1->es_lblk, es1->es_len)) {'''
assert text.count(old) == 2
# Flatten both original nested condition blocks, preserving debug and outputs.
for marker in ('static void __es_find_extent_range(', 'int ext4_es_lookup_extent('):
    begin = text.index(marker)
    start = text.index(old, begin)
    end = text.index('\n\t}\n', start) + len('\n\t}')
    block = text[start:end]
    middle = block[block.index('\n', block.index('if (in_range')) + 1:]
    middle = middle.rsplit('\n\t\t}\n\t}', 1)[0]
    middle = '\n'.join(line[1:] if line.startswith('\t') else line for line in middle.split('\n'))
    replacement = '\tes1 = READ_ONCE(tree->cache_es);\n\tif (es1 && in_range(lblk, es1->es_lblk, es1->es_len)) {\n' + middle + '\n\t}'
    text = text[:start] + replacement + text[end:]
text = text.replace('\t\ttree->cache_es = es1;', '\t\tWRITE_ONCE(tree->cache_es, es1);', 1)
begin = text.index('int ext4_es_lookup_extent(')
end = text.index('\nstruct rsvd_count {', begin)
function = text[begin:end]
function = function.replace('\tint found = 0;', '\tint found = 0;\n\tbool pointer_hit = false;\n\tbool profile = unlikely(READ_ONCE(es_lookup_profile)) &&\n\t\t!strcmp(current->comm, "es_bench");', 1)
function = function.replace('\t\tfound = 1;\n\t\tgoto out;', '\t\tfound = 1;\n\t\tpointer_hit = true;\n\t\tgoto out;', 1)
function = function.replace('out:\n\tstats =', 'out:\n\tif (profile)\n\t\tes_exp_record(tree, lblk, pointer_hit, found);\n\tstats =', 1)
function = function.replace('\t\tBUG_ON(!es1);', '''\t\tBUG_ON(!es1);
\t\tif (!pointer_hit && READ_ONCE(es_lookup_refresh)) {
\t\t\tWRITE_ONCE(tree->cache_es, es1);
\t\t\tif (profile)
\t\t\t\tatomic64_inc(&es_exp_refreshes);
\t\t}''', 1)
text = text[:begin] + function + text[end:]
destination = root / 'patches'
destination.mkdir(exist_ok=True)
patch = ''.join(difflib.unified_diff(original.splitlines(True), text.splitlines(True),
                                   fromfile='a/fs/ext4/extents_status.c', tofile='b/fs/ext4/extents_status.c'))
(destination / '0003-es-lookup-refresh.patch').write_text(patch)
(root / 'source/extents_status-refresh.c').write_text(text)
print(destination / '0003-es-lookup-refresh.patch')
