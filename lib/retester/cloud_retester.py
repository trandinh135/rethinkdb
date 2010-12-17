import subprocess, shlex, signal, os, time, shutil, tempfile, sys, traceback, types, gitroot, random, atexit
base_directory = os.path.dirname(os.path.join(os.getcwd(), sys.argv[0])) + "/../test"
use_local_retester = os.getenv("USE_CLOUD", "false") == "false"

# The following functions are for external use: setup_testing_nodes(), terminate_testing_nodes(). do_test_cloud(), report_cloud()
#   + the following functions imported from retester are compatible and can be used in combination with cloud tests: do_test()

# In order to enable running tests in Amazon's EC2, set the USE_CLOUD environment variable

# Please configure in ec2_configuration.py!
from cloud_config import ec2_configuration
import cloud_node_data

testing_nodes_ec2_instance_type = ec2_configuration.testing_nodes_ec2_instance_type
testing_nodes_ec2_count = ec2_configuration.testing_nodes_ec2_count
testing_nodes_ec2_image_name = ec2_configuration.testing_nodes_ec2_image_name
testing_nodes_ec2_image_user_name = ec2_configuration.testing_nodes_ec2_image_user_name
testing_nodes_ec2_key_pair_name = ec2_configuration.testing_nodes_ec2_key_pair_name
testing_nodes_ec2_security_group_name = ec2_configuration.testing_nodes_ec2_security_group_name
testing_nodes_ec2_region = ec2_configuration.testing_nodes_ec2_region
testing_nodes_ec2_access_key = ec2_configuration.testing_nodes_ec2_access_key
testing_nodes_ec2_private_key = ec2_configuration.testing_nodes_ec2_private_key

private_ssh_key_filename = ec2_configuration.private_ssh_key_filename

round_robin_locking_timeout = 2
wrapper_script_filename = "cloud_retester_run_test_wrapper.py" # must be just the name of the file, no path!

# END of configuration options



from stat import *
import paramiko # Using Paramiko for SSH2
import boto, boto.ec2 # Using Boto for AWS commands

from vcoptparse import *
from retester import *
import retester

reports = []
test_references = [] # contains pairs of the form (node, tmp-path)
testing_nodes_ec2_reservation = None
testing_nodes = []

next_node_to_issue_to = 0
node_allocation_tries_count = 0


class TestReference:
    def __init__(self, command):
        self.single_runs = []
        self.command = command

class TestingNode:
    def __init__(self, hostname, port, username, private_ssh_key_filename):
        self.hostname = hostname
        self.port = port
        self.username = username
        
        #print "Created TestingNode with hostname %s, port %i, username %s" % (hostname, port, username)
        
        # read private key from file to get access to the node
        if True: # Always use RSA for now
            self.private_ssh_key = paramiko.RSAKey(filename=private_ssh_key_filename)
        else:
            self.private_ssh_key = paramiko.DSSKey(filename=private_ssh_key_filename)
            
        self.global_lock_file = "/tmp/cloudtest_lock"
    
        system_random = random.SystemRandom()    
        self.global_build_path = "/tmp/cloudtest_build_" + str(system_random.randint(10000000, 99999999));
        self.global_bench_path = "/tmp/cloudtest_bench_" + str(system_random.randint(10000000, 99999999));
        self.global_test_path = "/tmp/cloudtest_test_" + str(system_random.randint(10000000, 99999999));
        #print "Installing build into %s\n" % self.global_build_path
        
        self.basedata_installed = False
        
        self.ssh_transport = None
        
    def __del__(self):
        if self.ssh_transport != None:
            self.ssh_transport.close()

        
    def get_transport(self, retry=True):
        if self.ssh_transport != None:
            return self.ssh_transport
    
        try:
            # open SSH transport
            self.ssh_transport = paramiko.Transport((self.hostname, self.port))
            self.ssh_transport.use_compression()
            self.ssh_transport.set_keepalive(60)
            self.ssh_transport.connect(username=self.username, pkey=self.private_ssh_key)
        except (IOError, EOFError, paramiko.SSHException) as e:
            self.ssh_transport = None
            time.sleep(120) # Wait a bit in case the network needs time to recover
            if retry:
                return self.get_transport(retry=False)
            else:
                raise e

        return self.ssh_transport
        
    
    # returns a tupel (return code, output)
    def run_command(self, command, retry = True):
        ssh_transport = self.get_transport()
        
        try:
            # open SSH channel
            ssh_channel = ssh_transport.open_session()
            
            # issue the command to the node
            ssh_channel.exec_command(command)
            
            # read back command result:
            # do not timeout while reading (probably default anyway?)
            ssh_channel.settimeout(None)
            # read output until we get an EOF
            command_output = ""
            output_read = ssh_channel.recv(4096) # No do-while loops in Python? wth?
            while len(output_read) > 0:
                command_output += output_read
                output_read = ssh_channel.recv(4096)
                
            # retrieve exit code
            command_exit_status = ssh_channel.recv_exit_status() # side effect: waits until command has finished
            
            ssh_channel.close()
            #self.ssh_transport.close()
            
            return (command_exit_status, command_output)
        except (IOError, EOFError, paramiko.SSHException) as e:
            self.ssh_transport = None
            if retry:
                return self.run_command(command, retry=False)
            else:
                raise e
           
        
    def put_file(self, local_path, destination_path, retry = True):
        ssh_transport = self.get_transport()
        
        try:
            # open SFTP session
            sftp_session = paramiko.SFTPClient.from_transport(ssh_transport)
        
            # do the operation
            sftp_session.put(local_path, destination_path)
            sftp_session.chmod(destination_path, os.stat(local_path)[ST_MODE])
        
            sftp_session.close()
        except (IOError, EOFError, paramiko.SSHException) as e:
            self.ssh_transport = None
            if retry:
                return self.put_file(local_path, destination_path, retry=False)
            else:
                raise e
        
        
    def get_file(self, remote_path, destination_path, retry = True):        
        ssh_transport = self.get_transport()
        
        try:
            # open SFTP session
            sftp_session = paramiko.SFTPClient.from_transport(ssh_transport)
            
            # do the operation
            sftp_session.get(remote_path, destination_path)
            
            sftp_session.close()
        except (IOError, EOFError, paramiko.SSHException) as e:
            self.ssh_transport = None
            if retry:
                return self.get_file(remote_path, destination_path, retry=False)
            else:
                raise e
        
        
    def put_directory(self, local_path, destination_path, retry = True):
        ssh_transport = self.get_transport()
    
        try:
            # open SFTP session    
            sftp_session = paramiko.SFTPClient.from_transport(ssh_transport)
            
            # do the operation
            for root, dirs, files in os.walk(local_path):
                for name in files:
                    sftp_session.put(os.path.join(root, name), os.path.join(destination_path + root[len(local_path):], name))
                    sftp_session.chmod(os.path.join(destination_path + root[len(local_path):], name), os.stat(os.path.join(root, name))[ST_MODE])
                for name in dirs:
                    #print "mk remote dir %s" % os.path.join(destination_path + root[len(local_path):], name)
                    sftp_session.mkdir(os.path.join(destination_path + root[len(local_path):], name))
            
            sftp_session.close()
        except (IOError, EOFError, paramiko.SSHException) as e:
            self.ssh_transport = None
            if retry:
                return self.put_directory(local_path, destination_path, retry=False)
            else:
                raise e
        
        
    def make_directory(self, remote_path, retry = True):
        ssh_transport = self.get_transport()

        try:
            # open SFTP session
            sftp_session = paramiko.SFTPClient.from_transport(ssh_transport)
            
            # do the operation
            sftp_session.mkdir(remote_path)
            
            sftp_session.close()
        except (IOError, EOFError, paramiko.SSHException) as e:
            self.ssh_transport = None
            if retry:
                return self.make_directory(remote_path, retry=False)
            else:
                raise e
        
        
    def make_directory_recursively(self, remote_path):
        # rely on mkdir command to do the work...
        mkdir_result = self.run_command("mkdir -p %s" % remote_path.replace(" ", "\\ "))
        
        if mkdir_result[0] != 0:
            print ("Unable to create directory")
            # TODO: Throw exception or something,,,
            
            
    def acquire_lock(self, locking_timeout = 0):
        lock_sleeptime = 1
        lock_command = "lockfile -%i -r -1 %s" % (lock_sleeptime, self.global_lock_file.replace(" ", "\' ")) # TODO: Better not use special characters in the lock filename with this incomplete escaping scheme...
        if locking_timeout > 0:
            lock_command = "lockfile -%i -r %i %s" % (lock_sleeptime, locking_timeout / lock_sleeptime, self.global_lock_file.replace(" ", "\' ")) # TODO: Better not use special characters in the lock filename with this incomplete escaping scheme...
        locking_result = self.run_command(lock_command)
        
        return locking_result[0] == 0
        
    def get_release_lock_command(self):
        return "rm -f %s" % self.global_lock_file.replace(" ", "\' ") # TODO: Better not use special characters in the lock filename with this incomplete escaping scheme...
        
    def release_lock(self):
        command_result = self.run_command(self.get_release_lock_command())
        if command_result[0] != 0:
            print "Unable to release lock (maybe the node wasn't locked before?)"
            # TODO: Throw exception or something,,,


def create_testing_nodes_from_reservation():
    global testing_nodes
    global testing_nodes_ec2_reservation
    global testing_nodes_ec2_image_user_name
    global private_ssh_key_filename
    
    for instance in testing_nodes_ec2_reservation.instances:
        if instance.state == "running":
            new_testing_node = TestingNode(instance.public_dns_name, 22, testing_nodes_ec2_image_user_name, private_ssh_key_filename)
            testing_nodes.append(new_testing_node)



def setup_testing_nodes():
    global testing_nodes
    global use_local_retester
    
    if use_local_retester:
        return
    
    atexit.register(terminate_testing_nodes)

    start_testing_nodes()
    
    # Do this on demand, such that we can start running tests on the first node while others still have to be initilized...
    #for node in testing_nodes:
    #    copy_basedata_to_testing_node(node)

def start_testing_nodes():
    global testing_nodes
    global testing_nodes_ec2_reservation
    global testing_nodes_ec2_image_name
    global testing_nodes_ec2_instance_type
    global testing_nodes_ec2_count
    global testing_nodes_ec2_key_pair_name
    global testing_nodes_ec2_security_group_name
    global testing_nodes_ec2_region
    global testing_nodes_ec2_access_key
    global testing_nodes_ec2_private_key
    global node_allocation_tries_count

    # Reserve nodes in EC2
    
    print "Spinning up %i testing nodes" % testing_nodes_ec2_count
    
    try:
        ec2_connection = boto.ec2.connect_to_region(testing_nodes_ec2_region, aws_access_key_id=testing_nodes_ec2_access_key, aws_secret_access_key=testing_nodes_ec2_private_key)
    
        # Query AWS to start all instances
        ec2_image = ec2_connection.get_image(testing_nodes_ec2_image_name)
        testing_nodes_ec2_reservation = ec2_image.run(min_count=testing_nodes_ec2_count, 
                                                      max_count=testing_nodes_ec2_count,
                                                      key_name=testing_nodes_ec2_key_pair_name,
                                                      security_groups=[testing_nodes_ec2_security_group_name],
                                                      instance_type=testing_nodes_ec2_instance_type)
        # query AWS to wait for all instances to be available
        for instance in testing_nodes_ec2_reservation.instances:
            while instance.state != "running":
                time.sleep(5)
                instance.update()
                if instance.state == "terminated":
                    # Something went wrong :-(
                    print "Could not allocate the requested number of nodes"
                    break
                    #terminate_testing_nodes()
                    #raise Exception("Could not allocate the requested number of nodes")
        create_testing_nodes_from_reservation()
    except:
        # We'll handle inability to spin up nodes in a moment
        pass
    
    if len(testing_nodes) == 0:
        terminate_testing_nodes()
        node_allocation_tries_count += 1
        if node_allocation_tries_count > 3:
            raise Exception("Could not allocate any testing nodes after %d tries, quitting..." % node_allocation_tries_count)
        else:
            print "Could not allocate any nodes, retrying..."
            time.sleep(30)
            start_testing_nodes()
            return
    
    # Give it another 120 seconds to start up...
    time.sleep(120)
    
    # Check that all testing nodes are up
    for node in testing_nodes:
        # send a testing command
        command_result = node.run_command("echo -n Are you up?")
        if command_result[1] != "Are you up?":
            print "Node %s is down!!" % node.hostname # TODO: Throw exception # TODO: This check fails with an exception anyway
        else:
            print "Node %s is up" % node.hostname
        # TODO: handle problems gracefully...


def terminate_testing_nodes():
    global testing_nodes
    global testing_nodes_ec2_reservation
    global testing_nodes_ec2_region
    global testing_nodes_ec2_access_key
    global testing_nodes_ec2_private_key

    if testing_nodes_ec2_reservation != None:
        print "Terminating EC2 nodes"
    
        ec2_connection = boto.ec2.connect_to_region(testing_nodes_ec2_region, aws_access_key_id=testing_nodes_ec2_access_key, aws_secret_access_key=testing_nodes_ec2_private_key)
    
        # Query AWS to stop all instances
        testing_nodes_ec2_reservation.stop_all()
        testing_nodes_ec2_reservation = None
    
    testing_nodes = None


def cleanup_testing_node(node):
    node.run_command("rm -rf " + node.global_build_path)
    node.run_command("rm -rf " + node.global_bench_path)
    node.run_command("rm -rf " + node.global_test_path)


def scp_basedata_to_testing_node(source_node, target_node):
    # Put private SSH key to source_node...
    source_node.run_command("rm -f private_ssh_key.pem")
    source_node.put_file(private_ssh_key_filename, "private_ssh_key.pem")
    command_result = source_node.run_command("chmod 500 private_ssh_key.pem")
    if command_result[0] != 0:
        print "Unable to change access mode of private SSH key on remote node"
        
    # Scp stuff to target node
    for path_to_copy in [("/tmp/cloudtest_libs", "/tmp/cloudtest_libs"), ("/tmp/cloudtest_bin", "/tmp/cloudtest_bin"), ("/tmp/cloudtest_python", "/tmp/cloudtest_python"), (source_node.global_build_path, target_node.global_build_path), (source_node.global_bench_path, target_node.global_bench_path), (source_node.global_test_path, target_node.global_test_path)]:
        command_result = source_node.run_command("scp -r -C -q -o stricthostkeychecking=no -P %i -i private_ssh_key.pem %s %s@%s:%s" % (target_node.port, path_to_copy[0], target_node.username, target_node.hostname, path_to_copy[1]))
        if command_result[0] != 0:
            print "Failed using scp to copy data from %s to %s: %s" % (source_node.hostname, target_node.hostname, command_result[1])
            return False
            
    target_node.basedata_installed = True
    return True


def copy_basedata_to_testing_node(node):
    global testing_nodes

    print "Sending base data to node %s" % node.hostname
    
    # Check if we can use scp_basedata_to_testing_node instead:
    for source_node in testing_nodes:
        if source_node.basedata_installed:
            print "Scp-ing base data from source node " + source_node.hostname
            if scp_basedata_to_testing_node(source_node, node):
                return
    
    # Copy dependencies as specified in ec2_configuration        
    node.make_directory_recursively("/tmp/cloudtest_libs")
    for (source_path, target_path) in ec2_configuration.cloudtest_lib_dependencies:
        node.make_directory_recursively("/tmp/cloudtest_libs/" + os.path.dirname(target_path))
        node.put_file(source_path, "/tmp/cloudtest_libs/" + target_path)
    
    node.make_directory_recursively("/tmp/cloudtest_bin")
    for (source_path, target_path) in ec2_configuration.cloudtest_bin_dependencies:
        node.make_directory_recursively("/tmp/cloudtest_bin/" + os.path.dirname(target_path))
        node.put_file(source_path, "/tmp/cloudtest_bin/" + target_path)
    command_result = node.run_command("chmod +x /tmp/cloudtest_bin/*")
    if command_result[0] != 0:
        print "Unable to make cloudtest_bin files executable"
    
    node.make_directory_recursively("/tmp/cloudtest_python")
    for (source_path, target_path) in ec2_configuration.cloudtest_python_dependencies:
        node.make_directory_recursively("/tmp/cloudtest_python/" + os.path.dirname(target_path))
        node.put_file(source_path, "/tmp/cloudtest_python/" + target_path)
    
    # Copy build hierarchy
    node.make_directory(node.global_build_path)
    #node.put_directory(base_directory + "/../build", node.global_build_path)
    # Just copy essential files to save time...
    for config in os.listdir(base_directory + "/../build"):
        if os.path.isdir(base_directory + "/../build/" + config):
            node.make_directory(node.global_build_path + "/" + config)
            node.put_file(base_directory + "/../build/" + config + "/rethinkdb", node.global_build_path + "/" + config + "/rethinkdb")
            node.put_file(base_directory + "/../build/" + config + "/rethinkdb-extract", node.global_build_path + "/" + config + "/rethinkdb-extract")
            #node.put_file(base_directory + "/../build/" + config + "/rethinkdb-fsck", node.global_build_path + "/" + config + "/rethinkdb-fsck")
            command_result = node.run_command("chmod +x " + node.global_build_path + "/" + config + "/*")
            if command_result[0] != 0:
                print "Unable to make rethinkdb executable"
        
    # Copy benchmark stuff
    node.make_directory(node.global_bench_path)
    node.make_directory(node.global_bench_path + "/stress-client")
    node.put_file(base_directory + "/../bench/stress-client/stress", node.global_bench_path + "/stress-client/stress")
    command_result = node.run_command("chmod +x " + node.global_bench_path + "/*/*")
    if command_result[0] != 0:
        print "Unable to make bench files executable"
        
    # Copy test hierarchy
    node.make_directory(node.global_test_path)
    node.put_directory(base_directory, node.global_test_path)
    
    # Install the wrapper script
    # TODO: Verify that this works!
    node.put_file(os.path.dirname(cloud_node_data.__file__) + "/" + wrapper_script_filename, "%s/%s" % (node.global_test_path, wrapper_script_filename));
    
    node.basedata_installed = True



def copy_per_test_data_to_testing_node(node, test_reference, test_script):    
    # Link build hierarchy
    command_result = node.run_command("ln -s %s cloud_retest/%s/build" % (node.global_build_path, test_reference))
    if command_result[0] != 0:
        print "Unable to link build environment"
        # TODO: Throw an exception
        
    # Link bench hierarchy
    command_result = node.run_command("ln -s %s cloud_retest/%s/bench" % (node.global_bench_path, test_reference))
    if command_result[0] != 0:
        print "Unable to link bench environment"
        # TODO: Throw an exception
    
    # copy over the global test hierarchy
    node.make_directory_recursively("cloud_retest/%s/test" % test_reference)    
    command_result = node.run_command("cp -af %s/* cloud_retest/%s/test" % (node.global_test_path, test_reference))
    if command_result[0] != 0:
        print "Unable to copy test environment"


def start_test_on_node(node, test_command, test_timeout = None, locking_timeout = 0):
    if locking_timeout == None:
        locking_timeout = 0

    #print ("trying to acquire lock with timeout %i" % locking_timeout)
    if node.acquire_lock(locking_timeout) == False:
        return False
    #print ("Got lock!")
    
    try:
        # Initialize node if not happened before...
        if node.basedata_installed == False:
            copy_basedata_to_testing_node(node)
        
        test_script = str.split(test_command)[0] # TODO: Does not allow for white spaces in script file name

        # Generate random reference
        system_random = random.SystemRandom()
        test_reference = "cloudtest_" + str(system_random.randint(10000000, 99999999))
        
        # Create test directory and check that it isn't taken
        directory_created = False
        while not directory_created:
            node.make_directory_recursively("cloud_retest")
            try:
                node.make_directory("cloud_retest/%s" % test_reference)
                directory_created = True
            except IOError:
                directory_created = False
                test_reference = "cloudtest_" + str(system_random.randint(10000000, 99999999)) # Try another reference
        
        print "Starting test with test reference %s on node %s" % (test_reference, node.hostname)
        
        # Prepare for test...
        copy_per_test_data_to_testing_node(node, test_reference, test_script)
        # Store test_command and test_timeout into files on the remote node for the wrapper script to pick it up
        command_result = node.run_command("echo -n %s > cloud_retest/%s/test/test_command" % (test_command, test_reference))
        if command_result[0] != 0:
            print "Unable to store command"
            # TODO: Throw an exception
        if test_timeout == None:
            command_result = node.run_command("echo -n \"\" > cloud_retest/%s/test/test_timeout" % (test_reference))
        else:
            command_result = node.run_command("echo -n %i > cloud_retest/%s/test/test_timeout" % (test_timeout, test_reference))
        if command_result[0] != 0:
            print "Unable to store timeout"
            # TODO: Throw an exception
            
        # Run test and release lock after it has finished
        command_result = node.run_command("sh -c \"nohup sh -c \\\"(cd %s; LD_LIBRARY_PATH=/tmp/cloudtest_libs:$LD_LIBRARY_PATH PATH=/tmp/cloudtest_bin:$PATH PYTHONPATH=/tmp/cloudtest_python:$PYTHONPATH VALGRIND_LIB=/tmp/cloudtest_libs/valgrind python %s; %s)&\\\" > /dev/null 2> /dev/null\"" % ("cloud_retest/%s/test" % test_reference, wrapper_script_filename.replace(" ", "\\ "), node.get_release_lock_command()))
            
    except (IOError, EOFError, paramiko.SSHException) as e:
        print "Starting test failed: %s" % e
        test_reference = "Failed"
        
        try:
            node.release_lock()
        except (IOError, EOFError, paramiko.SSHException):
            print "Unable to release lock on node %s. Node is now defunct." % node.hostname
        
            
    return (node, test_reference)


def get_report_for_test(test_reference):
    node = test_reference[0]
    result_result = node.run_command("cat cloud_retest/" + test_reference[1] + "/test/result_result")[1]
    result_description = node.run_command("cat cloud_retest/" + test_reference[1] + "/test/result_description")[1]
    if result_description == "":
        result_description = None
    
    result = Result(0.0, result_result, result_description)
    
    # Get running time
    try:
        result.running_time = float(node.run_command("cat cloud_retest/" + test_reference[1] + "/test/result_running_time")[1])
    except ValueError:
        print "Got invalid start_time for test %s" % test_reference[1]
        result.running_time = 0.0
    
    # Collect a few additional results into a temporary directory
    result.output_dir = SmartTemporaryDirectory("out_")
    for file_name in ["server_output.txt", "creator_output.txt", "test_output.txt"]:
        command_result = node.run_command("cat cloud_retest/" + test_reference[1] + "/test/output_from_test/" + file_name)
        if command_result[0] == 0:
            open(result.output_dir.path + "/" + file_name, 'w').write(command_result[1])
    
    # TODO: Also fetch network logs if any?    
    
    return result


def issue_test_to_some_node(test_command, test_timeout = 0):
    global testing_nodes
    global next_node_to_issue_to
    global round_robin_locking_timeout
    
    test_successfully_issued = False
    while test_successfully_issued == False:
        # wait for a limited amount of time until that node is free to get work
        test_reference = start_test_on_node(testing_nodes[next_node_to_issue_to], test_command, test_timeout, round_robin_locking_timeout)
        if test_reference != False:
            test_successfully_issued = True
        
        # use next node for the next try
        next_node_to_issue_to = (next_node_to_issue_to + 1) % len(testing_nodes)
    
    # return the reference required to retrieve results later, contains node and report dir
    return test_reference



def wait_for_nodes_to_finish():
    global testing_nodes
    
    print "Waiting for testing nodes to finish"
    
    for node in testing_nodes:
        try:
            node.acquire_lock()
            node.release_lock()
        except (IOError, EOFError, paramiko.SSHException) as e:
            print "Node %s is broken" % node.hostname


def collect_reports_from_nodes():
    global testing_nodes
    global reports
    global test_references
    
    print "Collecting reports"
    
    for test_reference in test_references:
        results = []
        for single_run in test_reference.single_runs:
            try:
                results.append(get_report_for_test(single_run))
            
                # Clean test (maybe preserve data instead?)
                node = single_run[0]
                node.run_command("rm -rf cloud_retest/%s" % test_reference)
            except (IOError, EOFError, paramiko.SSHException) as e:
                print "Unable to retrieve result for %s from node %s" % (single_run[1], single_run[0].hostname)
        
        reports.append((test_reference.command, results))
    
    # Clean node
    for node in testing_nodes:
        try:
            cleanup_testing_node(node)
        except (IOError, EOFError, paramiko.SSHException) as e:
            print "Unable to cleanup node %s: %s" % (node.hostname, e)
        
    terminate_testing_nodes()



# Safety stuff... (make sure that nodes get destroyed in EC2 eventually)
# This is not 100% fool-proof (id est does not catch all ways of killing the process), take care!
atexit.register(terminate_testing_nodes)



# modified variant of plain retester function...
# returns as soon as all repetitions of the test have been issued to some testing node
def do_test_cloud(cmd, cmd_args={}, cmd_format="gnu", repeat=1, timeout=60):
    global test_references
    global use_local_retester
    
    if use_local_retester:
        return do_test(cmd, cmd_args, cmd_format, repeat, timeout)
    
    # Build up the command line
    command = cmd
    for arg in cmd_args:
        command += " "
        # GNU cmd line builder
        if cmd_format == "gnu":
            if(isinstance(cmd_args[arg], types.BooleanType)):
                if cmd_args[arg]:
                    command += "--%s" % arg
            else:
                command += "--%s %s" % (arg, str(cmd_args[arg]))
        # Make cmd line builder
        elif cmd_format == "make":
            command += "%s=%s" % (arg, str(cmd_args[arg]))
        # Invalid cmd line builder
        else:
            print "Invalid command line formatter"
            raise NameError()
    
    # Run the test
    if repeat == 1: print "Running %r..." % command
    else: print "Running %r (repeating %d times)..." % (command, repeat)
    if timeout > 60: print "(This test may take up to %d seconds each time.)" % timeout
        
    test_reference = TestReference(command)
        
    for i in xrange(repeat):
        test_reference.single_runs.append(issue_test_to_some_node(command, timeout))
        
    test_references.append(test_reference)
        

# modified variant of plain retester function...
def report_cloud():
    global use_local_retester
    if use_local_retester:
        return report()

    wait_for_nodes_to_finish()

    # fill reports list
    collect_reports_from_nodes()

    # Invoke report() from plain retester to do the rest of the work...
    retester.reports.extend(reports)
    report()


